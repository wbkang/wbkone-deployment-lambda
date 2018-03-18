#/usr/bin/env python3

import subprocess
import boto3
import sys
import datetime
import time
import requests
import socket
import uuid
import traceback
import json
import os

cp = boto3.client('codepipeline')
APP_NAME = "wbkone4"
eb = boto3.client("elasticbeanstalk", region_name="us-east-1")
r53 = boto3.client('route53')
staging_host = 'staging.wbk.one'
TIMEOUT = 900


def report_success(job_id, message):
    print('Putting job success')
    print(message)
    cp.put_job_success_result(jobId=job_id)


def report_failure(job_id, message):
    print('Putting job failure')
    print(message)
    cp.put_job_failure_result(jobId=job_id, failureDetails={
        'message': message, 'type': 'JobFailed'})


def continue_later(job_id, context, msg):
    continuation_token = json.dumps(context)

    print('Putting job continuation, msg: %s, data: %r' % (msg, context))
    cp.put_job_success_result(
        jobId=job_id, continuationToken=continuation_token)


def handler(event, context):
    print("event:%r" % event)
    print("context:%r" % context)

    
    # event example
    # {'CodePipeline.job':
    #  {'accountId': '865135545601',
    #   'data': {
    #       'actionConfiguration': {
    #           'configuration': {
    #               'FunctionName': 'deploy-wbkone'}},
    #       'artifactCredentials': {
    #           'accessKeyId': '',
    #           'secretAccessKey': '',
    #           'sessionToken': ''},
    #       'inputArtifacts': [
    #           {'location': {
    #               's3Location': {
    #                   'bucketName': 'codepipeline-us-east-1-804545874954',
    #                   'objectKey': 'wbkone2-pipeline/MyAppBuild/aZvQ3jL'},
    #               'type': 'S3'},
    #            'name': 'MyAppBuild',
    #            'revision': None}],
    #       'outputArtifacts': []},
    #   'id': '0f8c4cec-0cd6-4864-a73b-4cad96ed6639'}}

    try:
        job = event['CodePipeline.job']
        job_id = job['id']

        # emergency brake
        if os.environ.get('ABORT', None) is not None:
            report_failure(job_id, "manual abort")
            return

        data = job['data']
        input_artifacts = data['inputArtifacts']
        continuation_token = data.get('continuationToken', None)
        # check application exists
        app = get_application(APP_NAME)
        if not app:
            raise Exception("APP %s is not found!" % APP_NAME)

        if not continuation_token:
            # assumption: there is only one s3 input artifact.
            s3loc = input_artifacts[0]['location']['s3Location']
            envs = find_old_environments(APP_NAME)

            if len(envs) == 0:
                # exception here, no old env, deploy & forget
                deploy_application(APP_NAME, (s3loc['bucketName'], s3loc['objectKey']))
                report_success(job_id, "Just deploying the initial application")
                return
            if len(envs) > 1:
                # i dont know what to do
                report_failure(job_id, "There are %d old environments, which is a lot more than 1" % len(envs))
                return

            env = envs[0]
            old_env_name = env['EnvironmentName']
            print("Found the old environment %s, deploying new environment" %
                  old_env_name, 1)
            
            env_name = deploy_application(
                APP_NAME, (s3loc['bucketName'], s3loc['objectKey']))
            continue_later(job_id, {'old_env':old_env_name, 'new_env':env_name, 'start_time': time.time()}, "Created environment")
            return

        ct_parsed = json.loads(continuation_token)
        old_env_name = ct_parsed['old_env']
        new_env_name = ct_parsed['new_env']
        start_time = ct_parsed['start_time']
        step = ct_parsed.get('step', 0)

        if (time.time() - start_time) > TIMEOUT:
            report_failure(job_id, "Timeout %r exceeded" % TIMEOUT)
            return

        def set_step(s):
            ct_parsed['step'] = s
            return ct_parsed

        if step <= 0:
            if is_environment_bad(new_env_name):
                report_failure(job_id, "Environment failed to stabilize")
                eb.terminate_environment(EnvironmentName=new_env_name)
                return
            if not is_environment_ready(new_env_name):
                continue_later(job_id, set_step(0), "Environment is not ready")
                return

        cname = get_cname(APP_NAME, new_env_name)
        print("CNAME is %s" % cname)

        if step <= 1:
            if not can_connect(cname, verify=False):
                continue_later(job_id, set_step(1), "Waiting for app to be ready")
                return
        
        if step <= 2:
            set_staging_cname(cname)
            continue_later(job_id, set_step(3), "staging cname change to propagate")
            return

        if step <= 3:
            if not is_staging_pointing_to(cname):
                continue_later(job_id, set_step(3), "Staging cname not propagated yet")
                return

        if not can_connect("staging.wbk.one", True):
            report_failure(job_id, "HTTPS broken?")
            eb.terminate_environment(EnvironmentName=new_env_name)
            return


        if step <= 4:
            eb.swap_environment_cnames(
                SourceEnvironmentName=new_env_name,
                DestinationEnvironmentName=old_env_name
            )
            continue_later(job_id, set_step(5), "Environment swap started")
            return
        
        if step <= 5:
            if not is_environment_ready(old_env_name):
                continue_later(job_id, set_step(5), "Environment swap in progress")
                return

        eb.terminate_environment(EnvironmentName=old_env_name)
        report_success(job_id, "Done!!!!")
    except Exception as ex:
        traceback.print_exc()
        cp.put_job_failure_result(
            jobId=job_id,
            failureDetails={
                'type': 'JobFailed',
                'message': str(ex)
            }
        )


def wait_until(cond, desc, timeout):
    begin_time = time.time()
    end_time = time.time() + timeout
    print("Waiting for [%s] up to [%r] seconds" % (desc, timeout))
    while time.time() < end_time:
        if cond():
            print("Condition [%s] good after %r seconds" %
                  (desc, time.time() - begin_time))
            return
        time.sleep(1)
    raise Exception("Timeout [%s] after %r seconds" % (desc, timeout))


def get_application_version(app_name, app_version):
    vers = eb.describe_application_versions(ApplicationName=app_name, VersionLabels=[
                                            app_version])['ApplicationVersions']
    return vers[0] if len(vers) > 0 else None


def is_app_version_processed(app_name, app_version):
    v = get_application_version(app_name, app_version)
    return v['Status'] == 'PROCESSED' if v else False


def is_environment_ready(env_name):
    health = eb.describe_environment_health(
        EnvironmentName=env_name,
        AttributeNames=['All']
    )
    return health['HealthStatus'] == "Ok" and health['Status'] == 'Ready'

def is_environment_bad(env_name):
    health = eb.describe_environment_health(
        EnvironmentName=env_name,
        AttributeNames=['All']
    )
    return health['HealthStatus'] == "Severe"

def find_old_environments(app_name):
    envs = [e
            for e
            in eb.describe_environments(ApplicationName=app_name)['Environments']
            if e.get('HealthStatus') == 'Ok']
    return envs


def can_connect(addr, verify):
    try:
        resp = requests.get("https://%s" % addr, verify=verify)
        if resp.status_code != 200:
            print("website [%s] is not working. Resp code is %r" %
                  (addr, resp.status_code))
            return False
        else:
            return True
    except:
        print("Some error while trying to connect to [%s], %r" % (
            addr, sys.exc_info()[0]))
        return False


def get_application(app_name):
    apps = eb.describe_applications(ApplicationNames=[APP_NAME])[
        "Applications"]
    if len(apps) == 1:
        return apps[0]
    assert len(apps) == 0
    return None


def get_cname(app_name, env_name):
    return eb.describe_environments(
        ApplicationName=app_name,
        EnvironmentNames=[env_name])["Environments"][0]['CNAME']


def deploy_application(app_name, s3bundle):
    """
    Args:
        s3bundle: (bucket, key)
    """
    dt = datetime.datetime.now()
    app_version = "app-" + dt.strftime("%Y%m%d%H%M%S")
    bucket, key = s3bundle
    eb.create_application_version(
        ApplicationName=app_name,
        VersionLabel=app_version,
        Description='string',
        SourceBundle={
            'S3Bucket': bucket,
            'S3Key': key
        },
        Process=True
    )

    wait_until(lambda: is_app_version_processed(
        APP_NAME, app_version), "app version processing", 10)

    env_name = "env-%s" % app_version

    eb.create_environment(
        ApplicationName=app_name,
        EnvironmentName=env_name,
        VersionLabel=app_version,
        SolutionStackName="64bit Amazon Linux 2017.09 v2.8.4 running Multi-container Docker 17.09.1-ce (Generic)",
        Tier={
            'Type': 'Standard',
            'Name': 'WebServer'
        }
    )

    return env_name


def set_staging_cname(cname):
    r53.change_resource_record_sets(
        HostedZoneId='ZJN26XW7SUDPS',
        ChangeBatch={
            'Comment': 'deployment test',
            'Changes': [
                {'Action': "UPSERT",
                 "ResourceRecordSet": {
                     'Name': '%s.' % staging_host,
                     'Type': 'CNAME',
                     'TTL': 1,
                     'ResourceRecords': [
                         {'Value': cname}
                     ]

                 }}
            ]
        }
    )

def is_staging_pointing_to(cname):
    try:
        hostname, _1, _2 = socket.gethostbyname_ex(
            staging_host)
        print("staging points to %s, cname is %s" % (hostname, cname))
        return hostname == cname
    except:
        return False


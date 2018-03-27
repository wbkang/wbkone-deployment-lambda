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

TAG_DEPLOYMENT_STATUS = 'wbkone:deployment-status'
DEPLOYMENT_STATUS_ACTIVE = "active"
DEPLOYMENT_STATUS_INPROGRESS = "inprogress"
DEPLOYMENT_STATUS_INACTIVE = "inactive"


def report_success(job_id, message):
    cp.put_job_success_result(jobId=job_id)
    return 'Putting job success: %s' % message


def report_failure(job_id, message):
    cp.put_job_failure_result(jobId=job_id,
                              failureDetails={'message': message, 'type': 'JobFailed'})
    return 'Putting job failure: %s' % message


def continue_later(job_id, context, msg):
    continuation_token = json.dumps(context)
    cp.put_job_success_result(
        jobId=job_id, continuationToken=continuation_token)
    return 'Putting job continuation, msg: %s, data: %r' % (msg, context)


def ct_mark_start_time(ct):
    ct['start_time'] = ct.get('start_time', time.time())


def ct_step(ct, step):
    ct['step'] = step
    return ct


def assert_one_env(envs):
    if len(envs) != 1:
        raise Exception("Not 1 environment found: %d" % len(envs))
    return envs[0]


def handle_deploy(job_id, data, continuation_token):
    input_artifacts = data['inputArtifacts']
    if len(input_artifacts) != 1:
        raise Exception("Only 1 input artifacts expected. Got %d" %
                        len(input_artifacts))
    s3loc = input_artifacts[0]['location']['s3Location']
    env = find_active_environment(APP_NAME)

    if env is None:
        # exception here, no old env, deploy & forget
        # deploy_application(APP_NAME, (s3loc['bucketName'], s3loc['objectKey']))
        # report_success(job_id, "Just deploying the initial application")
        return report_failure(job_id, "There is no active environment to deploy")

    active_env_name = env['EnvironmentName']
    print("Found the old environment %s, deploying new environment" %
          active_env_name)
    env_name = deploy_application(
        APP_NAME,
        (s3loc['bucketName'], s3loc['objectKey']))
    return report_success(job_id, "Deployment triggered")


def handle_healthcheck(job_id, data, ct):
    envs = find_environments_by_tags(
        APP_NAME, {TAG_DEPLOYMENT_STATUS: DEPLOYMENT_STATUS_INPROGRESS})
    env = assert_one_env(envs)
    env_name = env['EnvironmentName']
    if is_environment_bad(env_name):
        return report_failure(job_id, "Environment failed to stabilize. Check the bad environment")
    if not is_environment_ready(env_name):
        return continue_later(job_id, ct, "Environment is not ready")
    cname = get_cname(APP_NAME, env_name)
    print("CNAME is %s" % cname)
    if not can_connect(cname, verify=False):
        return continue_later(job_id, ct, "Waiting for app to be ready")
    return report_success(job_id, "new env healthcheck has passed")


def handle_httpscheck(job_id, data, ct):
    envs = find_environments_by_tags(
        APP_NAME, {TAG_DEPLOYMENT_STATUS: DEPLOYMENT_STATUS_INPROGRESS})
    env = assert_one_env(envs)
    env_name = env['EnvironmentName']
    cname = get_cname(APP_NAME, env_name)
    step = ct.get('step', 0)
    if step <= 0:
        set_staging_cname(cname)
        return continue_later(job_id, ct_step(ct, 1), "staging cname change to propagate")

    if step <= 1:
        if not is_staging_pointing_to(cname):
            return continue_later(job_id, ct_step(ct, 2), "Staging cname not propagated yet")

    if not can_connect("staging.wbk.one", True):
        return report_failure(job_id, "HTTPS broken? check the environment")
    return report_success(job_id, "httpscheck has passed")


def handle_swap(job_id, data, ct):
    active = assert_one_env(find_environments_by_tags(
        APP_NAME, {TAG_DEPLOYMENT_STATUS: DEPLOYMENT_STATUS_ACTIVE}))
    wip = assert_one_env(find_environments_by_tags(
        APP_NAME, {TAG_DEPLOYMENT_STATUS: DEPLOYMENT_STATUS_INPROGRESS}))
    step = ct.get('step', 0)
    if step <= 0:
        eb.swap_environment_cnames(
            SourceEnvironmentName=active['EnvironmentName'],
            DestinationEnvironmentName=wip['EnvironmentName']
        )
        return continue_later(job_id, ct_step(ct, 1), "Environment swap started")

    if step <= 1:
        if not is_environment_ready(active['EnvironmentName']):
            return continue_later(job_id, ct_step(ct, 1), "Environment swap in progress")

    eb.update_tags_for_resource(
        ResourceArn=wip['EnvironmentArn'],
        TagsToAdd=[
            {'Key': TAG_DEPLOYMENT_STATUS, "Value": DEPLOYMENT_STATUS_ACTIVE}
        ]
    )
    eb.update_tags_for_resource(
        ResourceArn=active['EnvironmentArn'],
        TagsToAdd=[
            {'Key': TAG_DEPLOYMENT_STATUS, "Value": DEPLOYMENT_STATUS_INACTIVE}
        ]
    )
    return report_success(job_id, "New environment is active.")


def handle_dispose(job_id, data, ct):
    old_env = assert_one_env(find_environments_by_tags(
        APP_NAME, {TAG_DEPLOYMENT_STATUS: DEPLOYMENT_STATUS_INACTIVE}))
    eb.terminate_environment(
        EnvironmentName=old_env['EnvironmentName']
    )
    return report_success(job_id, "Done!")


def handler(event, context):
    print("event:%r" % event)
    print("context:%r" % context)

    # event example
    # {'CodePipeline.job':
    #  {'accountId': '865135545601',
    #   'data': {
    #       'actionConfiguration': {
    #           'configuration': {
    #               'FunctionName': 'deploy-wbkone',
    #               'UserParameters': 'TestParameter'}},
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
        # check application exists
        app = get_application(APP_NAME)
        if not app:
            raise Exception("APP %s is not found!" % APP_NAME)

        job = event['CodePipeline.job']
        job_id = job['id']
        data = job['data']
        continuation_token = data.get('continuationToken', '{}')
        ct_parsed = json.loads(continuation_token)
        ct_mark_start_time(ct_parsed)
        # emergency brake
        if os.environ.get('ABORT', None) is not None:
            return report_failure(job_id, "manual abort")

        step_name = job['data']['actionConfiguration']['configuration']['UserParameters']

        step_functions = {
            'deploy': handle_deploy,
            'healthcheck': handle_healthcheck,
            'httpscheck': handle_httpscheck,
            'swap': handle_swap,
            'dispose': handle_dispose
        }

        fun = step_functions.get(step_name, None)
        if fun is None:
            raise Exception("Unknown step name [%s]" % step_name)

        if (time.time() - ct_parsed['start_time']) > TIMEOUT:
            report_failure(job_id, "Timeout %r exceeded" % TIMEOUT)
            return

        return fun(job_id, data, ct_parsed)
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


def find_environments_by_tags(app_name, tags_wanted):
    result = []

    def match_tag(tag, subset):
        for key, value in subset.items():
            if tag.get(key, None) != value:
                return False
        return True

    for e in eb.describe_environments(ApplicationName=app_name, IncludeDeleted=False, )['Environments']:
        if e['Status'] in {'Terminating', 'Terminated'}:
            continue
        tags = eb.list_tags_for_resource(ResourceArn=e['EnvironmentArn'])[
            'ResourceTags']
        tags_dict = {}
        for tag in tags:
            tags_dict[tag['Key']] = tag['Value']
        if match_tag(tags_dict, tags_wanted):
            result.append(e)

    return result


def find_active_environment(app_name):
    envs = find_environments_by_tags(
        app_name, {TAG_DEPLOYMENT_STATUS: DEPLOYMENT_STATUS_ACTIVE})
    if len(envs) > 1:
        raise Exception("More than 1 active environment found: %d" % len(envs))
    if len(envs) == 0:
        return None
    return envs[0]


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
    deployment_dt = datetime.datetime.now()
    bucket, key = s3bundle
    app_version = (bucket + ':' + key).replace("/", "_")

    versions = eb.describe_application_versions(
        ApplicationName=app_name,
        VersionLabels=[app_version]
    )['ApplicationVersions']
    if len(versions) > 1:
        raise Exception("More than 1 versions found? %r" % versions)
    if len(versions) == 0:
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

    env_name = "wbkone-%s" % deployment_dt.strftime("%Y%m%d%H%M%S")

    eb.create_environment(
        ApplicationName=app_name,
        EnvironmentName=env_name,
        VersionLabel=app_version,
        SolutionStackName="64bit Amazon Linux 2017.09 v2.8.4 running Multi-container Docker 17.09.1-ce (Generic)",
        Tier={
            'Type': 'Standard',
            'Name': 'WebServer'
        },
        Tags=[
            {
                'Key':TAG_DEPLOYMENT_STATUS,
                'Value':DEPLOYMENT_STATUS_INPROGRESS
            }
        ]
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

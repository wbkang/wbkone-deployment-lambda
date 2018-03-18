#!/bin/bash

scriptdir=$(readlink -f $(dirname $0))
cd $scriptdir; zip -r9 lambda.zip main.py
(cd env/lib/python3.5/site-packages; zip -r9 "$scriptdir/lambda.zip" .)

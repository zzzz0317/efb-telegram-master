#!/bin/sh
git pull
rm -rf build
python3.6 setup.py install
supervisorctl restart efb
#supervisorctl fg efb

#!/bin/bash
echo "creating elastalert indices"
/usr/local/bin/elastalert-create-index --config /data/elastalert/config.yaml --verbose
echo "Starting elastalert"
/usr/local/bin/elastalert --config /data/elastalert/config.yaml --verbose
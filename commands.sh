#!/bin/bash
echo "creating elastalert indices"
python /usr/local/bin/elastalert-create-index --config /data/elastalert/config.yaml --verbose
echo "Starting elastalert"
python /usr/local/bin/elastalert --config /data/elastalert/config.yaml --verbose
#!/bin/bash
echo "creating elastalert indices"
sh /usr/local/bin/elastalert-create-index --config /data/elastalert/config.yaml --verbose
echo "Starting elastalert"
sh /usr/local/bin/elastalert --config /data/elastalert/config.yaml --verbose
import requests
import json

headers = {
    'Content-Type': 'application/x-www-form-urlencoded',
}

requests.post('http://localhost:9123/',headers=headers, data="DROP DATABASE sherlock")
requests.post('http://localhost:9123/',headers=headers, data='CREATE DATABASE sherlock')

with open('./create_spans_v1', 'r') as file:
    data = file.read()
    res = requests.post('http://localhost:9123/',headers=headers, data=data)
    print(res.content)

with open('error_data', 'r') as file:
    data = file.read()
    res = requests.post('http://localhost:9123/?query=INSERT%20INTO%20sherlock.span%20FORMAT%20JSONEachRow',headers=headers, data=data)
    print(res.content)
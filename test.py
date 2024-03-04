import os
import yaml
def read_yaml():
    with open("test.yaml","r") as f:
       rules = yaml.safe_load(f) 
    rulesNameList = {"us-east-1":[],"eu-central-1":[],"ap-southeast-2":[],"ap-south-1":[]}
    if 'alert_params' in rules and rules['alert_params'] is not None:
        for ruleName,ruleDefinition in rules['alert_params'].items():
            if("is_enabled" in ruleDefinition and ruleDefinition["is_enabled"] == False):
                continue  #skiping rule if is_enabled field is false

            labels = {}
            if "labels" in ruleDefinition:
                labels = ruleDefinition["labels"]
                del ruleDefinition["labels"]
            string_labels = {key: str(val) for key, val in labels.items()}

            if string_labels and 'oncall' in string_labels:
                if string_labels['oncall'] == 'True':
                    string_labels['oncall'] = 'Yes'
                elif string_labels['oncall'] == 'False':
                    string_labels['oncall'] = 'No'

            ruleDefinition["labels"] = string_labels
    with open('output.yaml', 'w') as outfile:
                        yaml.dump(rules, outfile, default_flow_style=False)
    #yaml.dump(rules, "output.yaml", default_flow_style=False)
        

read_yaml()

# granny

https://learn.microsoft.com/en-us/legal/cognitive-services/openai/data-privacy

{
"name":"ContentLogging",
"value":"false"
}

# setup

```shell
sudo apt-get install terraform
pip install boto3  # Required for Python scripts
```

# deploy

```shell
terraform init
terraform plan
terraform apply
```

# Available Scripts

## AWS VPC Listing

List all VPCs in your AWS account:

```shell
# Using Bash
./list-vpcs.sh

# Using Python
./list-vpcs.py
```

## AWS Lambda Listing

List all Lambda functions across all AWS regions with their runtime versions:

```shell
# Using Bash
./list-lambdas.sh

# Using Python
./list-lambdas.py
```

Both scripts provide information about Lambda functions including their names, runtime environments (Node.js or Python versions), and function versions across all AWS regions.

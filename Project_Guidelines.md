# Granny Cloud Tools Collection - Project Guidelines

## 🚀 Project Overview

Welcome to the Granny Cloud Tools Collection! This project provides a comprehensive suite of AWS infrastructure management and DevOps automation tools designed to simplify cloud operations and improve deployment workflows.

## 📋 Table of Contents

1. [Project Purpose](#project-purpose)
2. [Architecture Overview](#architecture-overview)
3. [Development Guidelines](#development-guidelines)
4. [Security Standards](#security-standards)
5. [Tool-Specific Guidelines](#tool-specific-guidelines)
6. [Testing and Quality Assurance](#testing-and-quality-assurance)
7. [Deployment Best Practices](#deployment-best-practices)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Contributing Guidelines](#contributing-guidelines)

## 🎯 Project Purpose

Granny is designed to:
- Automate AWS infrastructure deployment and management
- Simplify static website hosting with CDN integration
- Extract and document database schemas
- Provide reliable disaster recovery mechanisms
- Integrate seamlessly with CI/CD pipelines

## 🏗️ Architecture Overview

### Core Components

```
┌─────────────────────────────────────────────────────────────┐
│                     Granny Cloud Tools                      │
├─────────────────────────┬───────────────────────────────────┤
│   Infrastructure Tools  │        DevOps Utilities          │
├─────────────────────────┼───────────────────────────────────┤
│ • S3 Website Setup      │ • MongoDB Schema Extractor      │
│ • CloudFront Manager    │ • Lambda Function Lister        │
│ • VPC Management        │ • Airtable Exporter             │
│ • SSL Certificate Auto  │ • Security Headers Manager      │
└─────────────────────────┴───────────────────────────────────┘
```

### Technology Stack

- **Languages**: Python 3.12+, Bash
- **Cloud Provider**: AWS (primary), Cloudflare (DNS/SSL)
- **Infrastructure as Code**: Terraform, CloudFormation
- **Package Management**: uv (Python), npm (Node.js)
- **CI/CD**: GitLab CI, GitHub Actions

## 📝 Development Guidelines

### Code Style and Standards

#### Python Development

1. **Style Guide**
   - Follow PEP 8 conventions
   - Use Black for code formatting
   - Run Ruff for linting
   - Maximum line length: 88 characters

2. **Type Annotations**
   ```python
   def process_bucket(bucket_name: str, region: str = "us-east-1") -> Dict[str, Any]:
       """Process S3 bucket configuration."""
       pass
   ```

3. **Error Handling**
   ```python
   try:
       response = s3_client.create_bucket(Bucket=bucket_name)
   except ClientError as e:
       logger.error(f"Failed to create bucket: {e}")
       raise
   ```

4. **Documentation**
   - Use Google-style docstrings
   - Include usage examples
   - Document all public APIs

#### Shell Scripting

1. **Script Structure**
   ```bash
   #!/bin/bash
   set -euo pipefail
   
   # Script: list-vpcs.sh
   # Purpose: List all VPCs across AWS regions
   # Usage: ./list-vpcs.sh [profile-name]
   ```

2. **Error Handling**
   ```bash
   trap 'echo "Error on line $LINENO"' ERR
   ```

3. **Variable Naming**
   - Use UPPERCASE for constants
   - Use lowercase for local variables
   - Use descriptive names

#### Terraform Standards

1. **Resource Naming**
   ```hcl
   resource "aws_s3_bucket" "granny_static_website" {
     bucket = "${var.project_name}-${var.environment}-website"
     # ...
   }
   ```

2. **Module Structure**
   ```
   modules/
   ├── s3-website/
   │   ├── main.tf
   │   ├── variables.tf
   │   ├── outputs.tf
   │   └── README.md
   ```

### Version Control

1. **Branch Strategy**
   - `main`: Production-ready code
   - `develop`: Integration branch
   - `feature/*`: New features
   - `fix/*`: Bug fixes
   - `docs/*`: Documentation updates

2. **Commit Messages**
   ```
   type(scope): brief description
   
   Detailed explanation of changes
   
   Fixes #123
   ```
   Types: feat, fix, docs, style, refactor, test, chore

3. **Pull Request Guidelines**
   - Include tests for new features
   - Update documentation
   - Pass all CI checks
   - Get at least one review

## 🔒 Security Standards

### Credential Management

1. **Environment Variables**
   ```bash
   export AWS_ACCESS_KEY_ID="your-key"
   export AWS_SECRET_ACCESS_KEY="your-secret"
   export AWS_REGION="us-east-1"
   ```

2. **AWS Profiles**
   ```bash
   aws configure --profile granny-prod
   ```

3. **Secrets Management**
   - Never commit credentials
   - Use AWS Secrets Manager for sensitive data
   - Rotate credentials regularly

### IAM Best Practices

1. **Least Privilege Principle**
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["s3:GetObject"],
       "Resource": "arn:aws:s3:::granny-bucket/*"
     }]
   }
   ```

2. **Service-Specific Roles**
   - Create separate roles for each service
   - Use temporary credentials via STS
   - Enable MFA for sensitive operations

### Network Security

1. **VPC Configuration**
   - Default CIDR: 10.0.0.0/16
   - Public subnets: 10.0.1.0/24, 10.0.2.0/24
   - Private subnets: 10.0.11.0/24, 10.0.12.0/24

2. **Security Groups**
   - Restrictive inbound rules
   - Specific port access only
   - Regular audit and review

## 🛠️ Tool-Specific Guidelines

### S3 Website Setup Tool

**Purpose**: Automate static website deployment with CDN

**Usage**:
```bash
python tools/setup_s3_website.py example.com \
  --cloudflare-ssl \
  --index-document index.html \
  --error-document error.html
```

**Best Practices**:
1. Always test with `--dry-run` first
2. Use CloudFlare SSL for easier setup
3. Enable versioning for content protection
4. Configure appropriate cache headers

### MongoDB Schema Extractor

**Purpose**: Document database schemas automatically

**Usage**:
```bash
python tools/mongodb_schema_extractor.py \
  -u "mongodb://localhost:27017" \
  -d mydb \
  -o ./docs/schemas/
```

**Output**:
- JSON schemas for each collection
- Sample data (anonymized)
- Mermaid ER diagrams
- Relationship documentation

### CloudFront Recovery Tools

**Purpose**: Recover from CloudFront distribution issues

**Common Issues**:
1. Orphaned distributions after stack deletion
2. Certificate validation failures
3. Origin access identity conflicts

**Recovery Process**:
```bash
# 1. List problematic distributions
./scripts/list-cloudfront-issues.sh

# 2. Run recovery script
./scripts/recover-cloudfront.sh distribution-id

# 3. Verify recovery
aws cloudfront get-distribution --id distribution-id
```

## 🧪 Testing and Quality Assurance

### Testing Strategy

1. **Unit Tests**
   ```python
   def test_bucket_creation():
       result = create_s3_bucket("test-bucket")
       assert result["ResponseMetadata"]["HTTPStatusCode"] == 200
   ```

2. **Integration Tests**
   - Test AWS API interactions
   - Verify resource creation
   - Check error handling

3. **End-to-End Tests**
   - Deploy test infrastructure
   - Validate functionality
   - Clean up resources

### Quality Checks

1. **Pre-commit Hooks**
   ```yaml
   repos:
     - repo: https://github.com/psf/black
       rev: 23.0.0
       hooks:
         - id: black
     - repo: https://github.com/charliermarsh/ruff-pre-commit
       rev: v0.0.261
       hooks:
         - id: ruff
   ```

2. **CI/CD Pipeline Checks**
   - Linting (Ruff, ShellCheck)
   - Security scanning (Bandit, Safety)
   - Unit test execution
   - Coverage reporting (>80%)

## 📦 Deployment Best Practices

### Infrastructure Deployment

1. **Terraform Workflow**
   ```bash
   # Initialize
   terraform init
   
   # Plan changes
   terraform plan -out=tfplan
   
   # Apply changes
   terraform apply tfplan
   
   # Destroy (careful!)
   terraform destroy
   ```

2. **Environment Management**
   - Use workspaces for environments
   - Separate state files per environment
   - Tag all resources appropriately

### Application Deployment

1. **Static Website Deployment**
   ```bash
   # Stage 1: Create infrastructure
   python tools/setup_s3_website.py staging.example.com
   
   # Stage 2: Deploy content
   aws s3 sync ./dist s3://staging.example.com
   
   # Stage 3: Invalidate cache
   aws cloudfront create-invalidation --distribution-id ABCD --paths "/*"
   ```

2. **Rollback Strategy**
   - Keep previous versions in S3
   - Use CloudFront behaviors for A/B testing
   - Implement health checks

### Monitoring and Alerts

1. **CloudWatch Metrics**
   - S3 bucket metrics
   - CloudFront distribution health
   - Lambda function errors
   - Cost anomalies

2. **Alerting**
   ```python
   # Example alert configuration
   alert_on_high_error_rate(
       threshold=5,  # errors per minute
       duration=300,  # 5 minutes
       sns_topic="arn:aws:sns:us-east-1:123:alerts"
   )
   ```

## 🔧 Troubleshooting Guide

### Common Issues and Solutions

#### S3 Website Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| 403 Forbidden | Bucket policy missing | Update bucket policy with public read |
| DNS not resolving | CloudFlare not configured | Check CloudFlare DNS settings |
| SSL certificate error | ACM cert not validated | Complete domain validation |

#### CloudFront Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Distribution stuck | Orphaned resources | Use recovery scripts |
| Cache not updating | Long TTL values | Create invalidation |
| Origin errors | S3 bucket deleted | Recreate bucket or update origin |

#### Terraform Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| State lock timeout | Concurrent operations | Wait or force unlock |
| Resource conflicts | Manual changes | Import resources to state |
| Plan differences | Drift detection | Run refresh before plan |

### Debug Commands

```bash
# Check AWS credentials
aws sts get-caller-identity

# List S3 buckets
aws s3 ls

# Get CloudFront distribution
aws cloudfront get-distribution --id ABCD

# Terraform state inspection
terraform state list
terraform state show aws_s3_bucket.static

# CloudWatch logs
aws logs tail /aws/lambda/function-name --follow
```

## 🤝 Contributing Guidelines

### Getting Started

1. **Fork the Repository**
   ```bash
   git clone https://github.com/yourusername/granny.git
   cd granny
   ```

2. **Set Up Development Environment**
   ```bash
   # Install Python dependencies
   uv venv
   source .venv/bin/activate
   uv pip install -e ".[dev]"
   
   # Install pre-commit hooks
   pre-commit install
   ```

3. **Create Feature Branch**
   ```bash
   git checkout -b feature/amazing-feature
   ```

### Contribution Process

1. **Write Code**
   - Follow coding standards
   - Add tests for new features
   - Update documentation

2. **Test Locally**
   ```bash
   # Run tests
   pytest tests/
   
   # Run linting
   ruff check .
   
   # Test scripts
   ./scripts/test-all.sh
   ```

3. **Submit Pull Request**
   - Clear description of changes
   - Link related issues
   - Include test results
   - Update CHANGELOG.md

### Code Review Checklist

- [ ] Code follows project style guide
- [ ] Tests pass and coverage maintained
- [ ] Documentation updated
- [ ] No hardcoded credentials
- [ ] Error handling implemented
- [ ] Performance considerations addressed
- [ ] Security best practices followed

## 📚 Additional Resources

### Documentation
- [AWS Best Practices](https://docs.aws.amazon.com/wellarchitected/)
- [Terraform Documentation](https://www.terraform.io/docs/)
- [Python Packaging Guide](https://packaging.python.org/)

### Tools and Libraries
- [boto3 Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/)
- [CloudFlare API](https://api.cloudflare.com/)
- [MongoDB Documentation](https://docs.mongodb.com/)

### Community
- Project Issues: [GitHub Issues](https://github.com/granny/issues)
- Discussions: [GitHub Discussions](https://github.com/granny/discussions)
- Stack Overflow: Tag with `granny-cloud-tools`

---

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🙏 Acknowledgments

- AWS for providing robust cloud services
- CloudFlare for DNS and SSL management
- The open-source community for invaluable tools and libraries

---

**Last Updated**: 2025-07-14

**Maintained By**: Granny Cloud Tools Team
# infra

Customer connection templates and our own hosting setup.

- aws-connect.yaml: CloudFormation read-only role for customers (ExternalId, explicit deny on
  secret values and data, Resource Explorer index). A copy is served by the API from
  apps/api/app/connectors/templates/ (a test keeps them identical).
- azure-connect.bicep: Reader on one resource group for a service principal (preview)
- k8s-readonly-rbac.yaml: read-only service account, no Secrets (preview)
- hosting/: our deploy (Docker Compose, Caddyfile)

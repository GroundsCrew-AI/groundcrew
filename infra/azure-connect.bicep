// Groundcrew read-only access to ONE resource group (preview: discovery runs on fixtures until
// TODO(azure-live)). Deploy at resource group scope:
//
//   az deployment group create --resource-group <rg> --template-file infra/azure-connect.bicep \
//     --parameters principalObjectId=<service principal object id>
//
// The service principal is created by the customer (az ad sp create-for-rbac --skip-assignment);
// this template only grants it Reader on the group. Reader can list resources and read their
// ARM properties, but cannot call list* actions that return keys, connection strings, or app
// settings, and cannot read Key Vault secrets (data plane). Remove access by deleting the role
// assignment (and the service principal's client secret).

targetScope = 'resourceGroup'

@description('Object ID of the service principal Groundcrew signs in as.')
param principalObjectId string

// Built-in Reader role.
var readerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')

resource groundcrewReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, principalObjectId, readerRoleId)
  properties: {
    roleDefinitionId: readerRoleId
    principalId: principalObjectId
    principalType: 'ServicePrincipal'
    description: 'Groundcrew read-only discovery (Resource Graph, ARM metadata)'
  }
}

output roleAssignmentId string = groundcrewReader.id

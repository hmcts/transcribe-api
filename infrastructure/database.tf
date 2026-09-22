# The database lives in the AKS VNet's delegated subnet, which sits in a
# different subscription from the application resources.
provider "azurerm" {
  features {}
  alias           = "postgres_network"
  subscription_id = var.aks_subscription_id
}

# Product-level Key Vault, created by transcribe-shared-infrastructure. The
# component writes its connection string back into it rather than handing
# credentials to the Helm chart, so the chart only ever names a secret.
data "azurerm_key_vault" "transcribe" {
  name                = "${var.product}-${var.env}"
  resource_group_name = "${var.product}-${var.env}"
}

module "postgresql" {
  providers = {
    azurerm.postgres_network = azurerm.postgres_network
  }

  source        = "git@github.com:hmcts/terraform-module-postgresql-flexible?ref=master"
  env           = var.env
  product       = var.product
  component     = var.component
  name          = var.product
  business_area = "cft"

  subnet_suffix = "expanded"
  common_tags   = var.common_tags

  pgsql_databases = [
    {
      name : var.product
    }
  ]

  pgsql_version        = var.pgsql_version
  admin_user_object_id = var.jenkins_AAD_objectId
}

# The application reads a single SQLAlchemy URL (DATABASE_CONNECTION_STRING),
# not a set of host/user/password parts, so the string is assembled here and
# mounted as one file by the chart. sslmode=require because the Flexible
# Server rejects unencrypted connections.
resource "azurerm_key_vault_secret" "database_connection_string" {
  name = "database-connection-string"
  value = format(
    "postgresql://%s:%s@%s:5432/%s?sslmode=require",
    module.postgresql.username,
    module.postgresql.password,
    module.postgresql.fqdn,
    var.product,
  )
  key_vault_id = data.azurerm_key_vault.transcribe.id
}

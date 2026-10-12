# Fernet key for encrypting stored webhook secrets (runtime/security.py). It
# is ours alone, not a credential for another system, so it is generated here
# rather than seeded by hand. A Fernet key is 32 random bytes, url-safe base64
# encoded; random_bytes gives standard base64, so swap the two characters that
# differ. The value is kept in Terraform state, so it does not change between
# runs; rotating it means replacing random_bytes.webhook_secret_encryption_key,
# after which webhook secrets encrypted under the old key no longer decrypt.
resource "random_bytes" "webhook_secret_encryption_key" {
  length = 32
}

resource "azurerm_key_vault_secret" "webhook_secret_encryption_key" {
  name         = "webhook-secret-encryption-key"
  value        = replace(replace(random_bytes.webhook_secret_encryption_key.base64, "+", "-"), "/", "_")
  key_vault_id = data.azurerm_key_vault.transcribe.id
}

path "secret/data/klyrow/staging/runtime/*" {
  capabilities = ["read"]
}
path "secret/metadata/klyrow/staging/runtime/*" {
  capabilities = ["read"]
}

# Klyrow and observability identities never receive the Odoo writer credential.
path "secret/data/codestra/staging/odoo-writer/*" {
  capabilities = ["deny"]
}

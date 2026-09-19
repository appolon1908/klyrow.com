"""Reusable Codestra API components shared by generated contracts."""
from pydantic import BaseModel, Field


class ApiError(BaseModel):
    code: str
    message: str
    request_id: str
    correlation_id: str
    details: dict = Field(default_factory=dict)


def components():
    return {
        "schemas": {"Error": ApiError.model_json_schema(), "Page": {
            "type": "object", "required": ["items", "next_cursor"],
            "properties": {"items": {"type": "array", "items": {}},
                           "next_cursor": {"type": ["string", "null"]}}}},
        "parameters": {
            "CorrelationId": {"name": "X-Correlation-Id", "in": "header", "required": False,
                              "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"}},
            "IdempotencyKey": {"name": "Idempotency-Key", "in": "header", "required": True,
                               "schema": {"type": "string", "minLength": 8, "maxLength": 200}},
            "Limit": {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
            "Cursor": {"name": "cursor", "in": "query", "schema": {"type": "string", "maxLength": 2048}},
        },
        "securitySchemes": {
            "keycloakOidc": {"type": "openIdConnect", "openIdConnectUrl": "https://auth.codestra.co/realms/codestra/.well-known/openid-configuration"},
            "productApiKey": {"type": "apiKey", "in": "header", "name": "Authorization",
                              "description": "Bearer <product API key>; stored verifiers are never returned."},
            "serviceBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
            "serviceMtls": {"type": "mutualTLS"},
        },
    }

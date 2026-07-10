"""Adapty subscription webhook (ADR-029, modules/billing-adapty).

POST /v1/billing/adapty/webhook — server-to-server webhook from Adapty. Static bearer auth,
raw-body defensive parsing (always 2xx after auth), single-transaction idempotent apply
(subscriptions upsert + credit grant + audit).
"""

"""RU billing webhook — broadapps/YooKassa in CloudPayments format (ADR-050,
modules/billing-cloudpayments).

POST /v1/billing/cloudpayments/webhook — server-to-server payment callback from the broadapps
aggregator (CloudPayments wire format). Static bearer auth, raw-body defensive parsing (always 2xx
after auth), single-transaction idempotent apply (subscriptions upsert | one-time token grant +
credit grant + audit). Isolated from Adapty / StoreKit / BYOK: own secret, parser, dedup table and
ledger namespace (``cp-txn:*``). Card data is never read into business logic, logged, or persisted.
"""

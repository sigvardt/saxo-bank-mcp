# Privacy and evidence

Use this reference for privacy-safe user wording, validation errors, no-purchase evidence, publication, and incident records.

## Validation errors

Validation errors name field/rule, never submitted value.

Safe examples:

- `quantity must be greater than zero`
- `price must match tick size`
- `path_parameters.OrderId is required`
- `account selector must come from the current account list`
- `authorization field is not allowed in evidence`

Unsafe examples:

- Echoing the submitted price, order ID, account key, token, authorization header, disclaimer token, approval statement, or raw broker body.
- Copying a Saxo validation message that contains the submitted value.
- Returning `DisplayName` when an alias works.

## Account alias policy

Account numbers are usable internal selectors and not inherently secret, but user-facing output prefers alias.

Account-number-shaped text alone is diagnostic unless it is associated with a submitted value, raw account field, or secret-bearing key.

Use aliases such as:

- `selected SIM account`
- `selected LIVE account`
- `account alias 1`
- `process-scoped account reference`

Do not publish raw account keys, client keys, account group keys, `DisplayName`, raw account fields, balances, raw order IDs, raw position IDs, private financial details, or raw broker payloads unless the user explicitly needs a safe internal selector and the tool returned that selector for the current process.

## Safe evidence fields

Evidence may contain:

- environment label
- logical tool ID
- operation ID
- path template
- status
- retry class
- next action
- alias
- sanitized event count
- request fingerprint
- preview-token fingerprint
- response hash
- HMAC balance fingerprint
- ledger completeness
- eviction count
- negative-proof flag
- before/after equality boolean
- process ID for task-owned cleanup
- command return code

Evidence must not contain:

- access token
- refresh token
- authorization header
- approval authorization binding
- preview token
- disclaimer token
- account key
- client key
- raw account field
- `DisplayName`
- submitted validation value
- raw broker payload
- raw URL with selectors
- private path
- private financial value
- prompt-injected secret text

## No-purchase proof

Only complete non-evicted ledger with `negative_proof_available=true` supports absence proof.

An incomplete ledger cannot prove that no request or purchase occurred.

An evicted ledger cannot prove that no request or purchase occurred.

No-purchase wording also needs matching before/after state evidence for the scoped task. Ledger evidence alone cannot prove external broker state or actions by another client.

## Publication rule

Preserve incident evidence without exposing secrets. If a required artifact fails the privacy scan, publish only the failure class, safe status, and next cleanup or recovery action.

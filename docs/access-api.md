# Access API

By default the server lets in the players listed in `VF_USERS`, without limits. To run it for many people with accounts, plans and daily limits, point it at a service of your own with `VF_ACCESS_URL`. The server then keeps no list of users: it asks that service who a token or a Telegram user is, whether each message may be processed, and tells it what each message cost. Billing, sign-up and the account page live in that service, not here.

```
VF_ACCESS_URL=http://accounts:8080/access
VF_ACCESS_SECRET=<a long random string, the same in both services>
# VF_ACCESS_TIMEOUT_S=2
```

`VF_USERS` and `VF_ACCESS_URL` cannot be set together.

## Requests

Every call is `POST <VF_ACCESS_URL>/<call>` with a JSON body and two headers:

| Header | Value |
|---|---|
| `X-VF-Timestamp` | Unix time in seconds |
| `X-VF-Signature` | `sha256=` + hex HMAC-SHA256 of `<timestamp>.<body>` with `VF_ACCESS_SECRET` |

Check the signature with a constant-time compare and refuse timestamps more than a few minutes old. Answer `200` with a JSON object. Any other status, a timeout (`VF_ACCESS_TIMEOUT_S`, 2 s by default) or a broken answer counts as "unavailable": nothing is processed, the agent and the bot tell the player to try again in a minute.

Player ids are integers chosen by the service (an account id). The server stores the player's settings under that id.

## `resolve`

Who is this agent token or Telegram user. The server asks on every agent connection and for Telegram updates (cached for 60 s).

```json
{"agent_token": "..."}
{"telegram_id": 123456789, "lang": "de"}
```

Known:

```json
{"user_id": 17, "account": {...}}
```

Unknown or refused: `error` is `unknown` (no account; the bot invites the person to sign in) or `revoked` (the token was revoked), `link` is the page where they can fix it.

```json
{"error": "unknown", "link": "https://example.com/account"}
```

The agent gets `401` with `{"error", "link"}` for a refused token and `503` when the service is unavailable.

## `check`

Asked before speech recognition, for every voice or text message.

```json
{"user_id": 17, "source": "agent" | "telegram", "kind": "voice" | "text", "audio_s": 3.2}
```

```json
{"allow": "claude", "check_id": "c-81f2", "account": {...}}
{"allow": "transcribe", "check_id": "c-81f3", "reason": "limit", "account": {...}}
{"allow": "no", "reason": "no_access", "link": "https://example.com/account"}
```

| `allow` | What happens |
|---|---|
| `claude` | speech recognition, then Claude picks the channel and fixes the text |
| `transcribe` | speech recognition, then the keyword rules pick the channel; with `reason: "limit"` the player is told that today's AI messages are used up |
| `no` | nothing; the player sees the reason and a button to `link` |

Reasons the server has texts for: `unknown`, `revoked`, `no_access`, `limit`, `rate`, `unavailable`. Any other reason reads as `no_access`. `revoked` also closes the agent's connection (code 4001).

Besides the service's answer, the server refuses more than `VF_RATE_PER_MIN` messages per player per minute (20 by default) with reason `rate`.

## `usage`

Sent after each checked message, in the background. `check_id` is the one from `check`.

```json
{
  "check_id": "c-81f2", "user_id": 17, "source": "agent",
  "outcome": "delivered", "ai": true, "audio_s": 3.2,
  "model": "claude-haiku-5-5", "input_tokens": 1830, "output_tokens": 41,
  "cache_read_tokens": 1600, "cache_write_tokens": 0
}
```

| `outcome` | Meaning |
|---|---|
| `delivered` | typed into the game |
| `routed` | shown in Telegram with a confirm button; the player sends it with a tap |
| `failed` | the agent could not type it (offline, game not focused, timeout) |
| `skipped` | not a chat message |
| `empty` | no words heard |
| `stt_error` | speech recognition failed |

`ai` is true when Claude picked the channel. To count "AI messages", count reports with `ai: true` and outcome `delivered` or `routed`. The token fields come from Claude's `response.usage`; they are zero when Claude was not asked. The answer may carry a new `account`.

## `account`

Whatever the service puts in `account` goes to the player's agent as is, as `{"type": "account", ...}`: on connection and after each `check` and `usage` that returns one. The Windows app shows these fields if present:

| Field | Example | Shown as |
|---|---|---|
| `plan` | `"Full access"` | the plan name |
| `ai_left`, `ai_limit` | `73`, `100` | AI messages left today |
| `until` | `1767225600` | paid until (Unix time) |
| `link` | `"https://example.com/account"` | the "Account" button |

## `payment`: Telegram Stars

With `VF_ACCESS_URL` the bot gets `/buy`, `/terms` and `/paysupport`. `/buy` sells a subscription for Telegram Stars that Telegram renews every 30 days. The bot only passes the steps on; the service decides the price and keeps the subscription. All three steps go to `payment` with a `step` field.

`offer`: the player sent `/buy`.

```json
{"step": "offer", "user_id": 17, "lang": "en"}
```

```json
{"title": "Full access", "description": "100 AI messages a day", "stars": 330, "payload": "order-5521"}
```

or `{"error": "no_access", "link": "..."}`. The title is cut to 32 characters, the description to 255, the payload must be at most 128 bytes.

`pre_checkout`: Telegram asks whether to take the money. Answer within a few seconds.

```json
{"step": "pre_checkout", "user_id": 17, "payload": "order-5521", "currency": "XTR", "total_amount": 330}
```

```json
{"ok": true}
```

`paid`: the money arrived. Renewals come the same way, with `is_recurring: true`. `user_id` is `0` if the player could not be resolved at that moment; use `payload` and `telegram_id`. The server sends this report up to four times and then writes it to the log, so make it idempotent by `telegram_payment_charge_id`.

```json
{
  "step": "paid", "user_id": 17, "telegram_id": 123456789,
  "payload": "order-5521", "currency": "XTR", "total_amount": 330,
  "telegram_payment_charge_id": "...", "is_recurring": true, "is_first_recurring": true,
  "subscription_expiration_date": 1767225600
}
```

Cancelling and refunds are up to the service: it can call the Bot API (`editUserStarSubscription`, `refundStarPayment`) with the same bot token. Set `VF_TERMS_URL` and `VF_PAY_SUPPORT` for the answers to `/terms` and `/paysupport`, which Telegram requires from bots that sell.

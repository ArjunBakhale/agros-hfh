# Audit: existing Greenlight voice components and how they compare to this service

Source repo: `ArjunBakhale/greenlight` (private). Reviewed while building this service.
No code was copied.

| Greenlight component | What it does | Gap compared to the HFH design |
| --- | --- | --- |
| `apps/web/src/voice-agent/vapi.ts` | Starts outbound calls through Vapi | Single vendor (Vapi) with no provider abstraction. The HFH design calls for Twilio + LiveKit + Deepgram + Bedrock + Cartesia. |
| `apps/web/src/voice-agent/prompt.ts` | LLM system prompt with All of Us/NIH wording | The LLM writes its own speech. Here, speech comes only from the approved script and the LLM only labels replies. |
| `apps/web/src/voice-agent/events.ts`, `app/api/voice-agent/vapi/events/route.ts` | Vapi webhook intake | Uses Vapi's own signature scheme and has no replay window. Here: HMAC with a timestamp, idempotent `event_id`, order-independent replay, and quarantine for uncorrelated events. |
| `apps/web/src/voice-agent/recording.ts` | Recording/transcript handling | The HFH design keeps recording and transcripts off by default. This service has no code path that stores either. |
| `apps/web/app/voice-agent/page.tsx` | Demo page | Replaced by the coordinator dashboard (pipeline, handoffs, failures, export, audit). |
| `supabase/migrations/0053`–`0058` | Voice demo tables plus consent, age and geography status | Greenlight's version uses Supabase RLS on JWT claims. This service uses the same model, implemented as plain Postgres RLS on `app.tenant_id` with a non-owner app role. |
| `docs/engineering-specs/voice-participant-outreach.md` | Spec for the All of Us farm outreach product | A separate product. Not assumed to apply to HFH. |

Conventions carried over from Greenlight's `AGENTS.md`: append-only, hash-chained audit
written through one writer; explicit tenant scoping for every query; immutable
`tenant_id`; handoffs and dispositions change only through explicit, audited actions.

# Voice enrollment: data flow and safety posture

## Data boundary
- **Contact layer only.** A contact is made of an opaque `contact_token`, an E.164 phone
  number, a language, an optional age band, and boolean eligibility flags
  (`adult`, `on_or_preparing_for_dialysis`). The CSV import rejects name, DOB, MRN, email,
  diagnosis, and notes columns.
- The service has no connection to EHR, genomic, Researcher Workbench, Terra, or federal
  research systems.
- Without `VOICE_REAL_CONTACTS_APPROVED`, imports only accept fixture contacts with
  fictional NANP 555-0100..0199 numbers or numbers on the live allowlist.

## What is stored
| Data | Stored | Where |
| --- | --- | --- |
| Phone number | yes (to dial) | `voice_contacts.phone_e164`. The dashboard shows it masked (`***0101`). It is never in exports or audit. |
| Participant speech (audio) | **no** | Handled in memory by Deepgram via LiveKit, then discarded |
| Transcript text | **no** | The agent sends only `{seq,node,intent}`. The server allowlists payload fields and drops `text`/`audio`. |
| Structured answers | yes | `voice_call_attempts.answers` (e.g. `interest: yes`, `preferred_window: afternoon`) |
| Disposition, handoffs | yes | `voice_call_attempts`, `voice_contacts`, `voice_handoffs` |
| Audit events | yes, append-only | `audit_events` (hash-chained, with phone/email/text keys and patterns redacted) |

## Subprocessors (live mode only)
Twilio (PSTN), LiveKit Cloud (media and SIP), Deepgram (STT), Cartesia (TTS), and AWS
Bedrock (intent labels only). These vendors process call audio or derived text in
transit. **BAAs and subprocessor approval are required before any real participant call.**

## Agent behaviour limits
- It can only speak strings from the approved script JSON. The LLM output is a single
  label from a closed set, and anything else is treated as `unclear`.
- Clinical questions, distress, requests for a human, eligibility disputes, other
  languages, and repeated unclear replies all end the call politely and open a
  `human_review` handoff.
- The agent does not give medical advice, decide eligibility, or collect consent. A
  coordinator call is always the next step.
- Opt-out and wrong-number outcomes set `do_not_call`.

## Tenant isolation and authorization
- The app connects as `agros_app` (non-owner, `NOBYPASSRLS`). Every tenant table has RLS
  on `app.tenant_id`, which only `db.with_tenant()` sets, for one transaction at a time.
  With no tenant set, queries return zero rows.
- Cross-tenant entry points are three `SECURITY DEFINER` functions: login lookup,
  webhook correlation by `(provider, provider_call_id)`, and the timeout sweep.
- Roles: admin/manager can do everything; cta can read, start calls and resolve handoffs;
  viewer is read-only. Cookie sessions require a double-submit CSRF token.

## Decisions requiring human sign-off
1. The script content, the call-window policy, and the voicemail policy (HFH clinical and
   IRB review).
2. BAAs and approval for Twilio, LiveKit, Deepgram, Cartesia, and Bedrock.
3. Whether recordings or transcripts are ever retained, and for how long.
4. Turning on real contacts (`VOICE_REAL_CONTACTS_APPROVED`) and moving beyond the
   internal allowlist.
5. The consent path (e-consent link or warm transfer) for M1.
6. Data retention for `voice_contacts.phone_e164` after the campaign closes.

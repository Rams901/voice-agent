# Master variable bank

Every field the voice agent can ask about, collect, infer, or validate across
all dealership scenarios. This is the reference for two questions:

1. **What should the agent ask?** (the `Ask?` column — and how it should ask)
2. **Where does the answer come from, and who is allowed to be the authority?**

The second question matters more than the first. A booking fails not because the
agent asked the wrong question, but because it treated its own guess as a fact.
So every variable below declares a **source of truth**, and the rule is:

> The agent may *propose* a value. Only a deterministic source may *settle* one.

## Source-of-truth codes

| Code | Meaning | Who is authoritative |
|---|---|---|
| `CALLER` | Only the customer knows this | The caller — must be captured and confirmed |
| `DMS` | Lives in the dealer's system of record | The DMS lookup — never the model |
| `TOOL` | Computed or derived by deterministic code | The tool/service that computed it |
| `CONFIG` | Dealer/market configuration | The dealer profile — injected, never invented |
| `CALL` | Telephony metadata | The telephony platform |
| `INFER` | Model judgement (soft, low-stakes) | The model — acceptable only when non-binding |

## High-stakes fields: the capture contract

These are the fields where a single wrong character invalidates the record. Each
one gets the **five-stage capture pipeline**: constrain → capture (phonetic) →
read back → validate → confirm. Never collapsed into one step.

| Variable | Format | Validation | Read-back style |
|---|---|---|---|
| `registration_plate` | Country mask (GB: `LLDDLLL`, NL: `XX-999-X`…) | Mask + record lookup | Phonetic, grouped by mask run |
| `vin` | 17 chars, no I/O/Q, ISO 3779 check digit | Structure + check digit | Phonetic, grouped 3-6-8 |
| `mileage` | Integer | Plausibility bound | Digits with spoken grouping |
| `phone_number` | E.164 | Country code + length | Digit triplets |
| `email` | RFC-ish | Regex + spoken-form repair | Spell local part back |
| `postcode` | Country format | Mask | Character by character |
| `booking_date` / `time` | ISO datetime | Must exist in DMS availability | Natural language + explicit |
| `payment_card_*` | — | **Never captured on voice.** Transfer instead. | — |

---

## 1. Call and identity

| Variable | Type | Ask? | Source | Notes |
|---|---|---|---|---|
| `call_id` | string | no | `CALL` | Telephony id; used for tracing |
| `from_number` | string (E.164) | no | `CALL` | Primary identity key for inbound |
| `to_number` | string | no | `CALL` | Identifies which dealer was dialled |
| `direction` | enum | no | `CALL` | inbound / outbound |
| `caller_first_name` | string | yes | `CALLER` | Confirm, don't ask if known |
| `caller_last_name` | string | yes | `CALLER` | Spelling variant matters in some markets |
| `preferred_language` | enum | conditional | `CONFIG` | Start in dealer default; switch on request |
| `recording_consent` | bool | yes | `CALLER` | **Required before anything else** in consent markets |
| `is_existing_customer` | bool | inferred | `DMS` | From `from_number` → customer record |
| `customer_id` | string | no | `DMS` | Set from the lookup, never from the model |
| `preferred_contact_channel` | enum | yes | `CALLER` | sms / whatsapp / email / voice |
| `email` | string | conditional | `CALLER` | Only if written confirmation is needed |
| `callback_consent` | bool | yes | `CALLER` | Needed for outbound follow-up (GDPR) |

## 2. Vehicle identification

| Variable | Type | Ask? | Source | Notes |
|---|---|---|---|---|
| `registration_plate` | string | yes | `CALLER` → `DMS` | **Highest-error field on the call** |
| `plate_normalised` | string | derived | `TOOL` | After mask repair |
| `plate_valid` | bool | derived | `TOOL` | Format match |
| `plate_coercions` | list | derived | `TOOL` | Repaired positions — must be confirmed |
| `plate_confirmed_by_caller` | bool | yes | `CALLER` | **Gate: no lookup before true** |
| `vin` | string | conditional | `CALLER` | Fallback when the plate is not on record |
| `vehicle_id` | string | no | `DMS` | DMS primary key |
| `vehicle_make` / `_model` / `_year` | string/int | no | `DMS` | Never invented |
| `vehicle_colour` | string | no | `DMS` | **Used to disambiguate**, not to identify |
| `vehicle_fuel_type` | enum | no | `DMS` | petrol / diesel / hybrid / EV |
| `vehicle_mileage` | int | yes | `CALLER` → `DMS` | Caller's reading vs last recorded differ |
| `vehicle_summary` | string | no | `TOOL` | Human-readable: "2021 BMW 3 Series, grey" |
| `vehicle_match_count` | int | no | `DMS` | **>1 means ask, never pick** |
| `is_new_customer` | bool | yes | `CALLER` | No record found path |

## 3. Service booking (core scenario)

| Variable | Type | Ask? | Source | Notes |
|---|---|---|---|---|
| `service_type` | enum | yes | `CALLER` → `DMS` | Mapped to a DMS service product id |
| `service_product_id` | string | no | `DMS` | DMS-side identifier |
| `symptom_description` | string (free) | yes | `CALLER` | Capture verbatim; do not diagnose |
| `warning_light_on` | bool | yes | `CALLER` | Safety-relevant; routes to diagnostics |
| `is_driveable` | bool | conditional | `CALLER` | Determines recovery vs booking |
| `urgency` | enum | inferred | `INFER` | routine / soon / safety_critical |
| `preferred_date` | date (ISO) | yes | `CALLER` | Converted from natural language |
| `preferred_time_window` | enum | yes | `CALLER` | morning / afternoon / specific |
| `date_range_from` / `_to` | date | derived | `TOOL` | Widened if no slots |
| `offered_slots` | list | no | `DMS` | **Only source of offerable times** |
| `chosen_slot` | datetime | yes | `CALLER` | Must be one of `offered_slots` |
| `appointment_datetime` | datetime | derived | `DMS` | What the DMS actually stored |
| `duration_minutes` | int | no | `DMS` | From the service product |
| `transport_option` | enum | yes | `CALLER` | wait_on_site / loan_car / collection / none |
| `collection_address` | string | conditional | `CALLER` | Only if collection chosen |
| `advisor_id` / `advisor_name` | string | no | `DMS` | Assigned by the DMS, not the agent |
| `special_instructions` | string | yes | `CALLER` | Accessibility, pets, timing constraints |
| `estimate_amount` | money | no | `DMS` | **Never quoted from memory** |
| `estimate_is_indicative` | bool | derived | `CONFIG` | Drives the "team will confirm" caveat |
| `booking_reference` | string | no | `DMS` | Read from the write, not composed |
| `appointment_id` | string | no | `DMS` | Used for write verification |
| `booking_confirmed` | bool | yes | `CALLER` | The confirmation gate |
| `write_verified` | bool | no | `TOOL` | Read-back from the DMS after writing |
| `reminder_opt_in` | bool | yes | `CALLER` | Consent for reminder messages |

## 4. Rental / loan car

| Variable | Type | Ask? | Source | Notes |
|---|---|---|---|---|
| `loan_car_offered` | bool | no | `CONFIG` | Dealer capability — some don't offer it |
| `rental_required` | bool | yes | `CALLER` | |
| `rental_start_date` / `_end_date` | date | yes | `CALLER` | |
| `rental_days` | int | derived | `TOOL` | |
| `rental_vehicle_class` | enum | yes | `CALLER` | compact / estate / SUV / van / automatic |
| `rental_transmission` | enum | yes | `CALLER` | Important for licence eligibility |
| `rental_driver_age` | int | yes | `CALLER` | Affects insurance eligibility |
| `rental_licence_number` | string | yes | `CALLER` | Captured, not judged |
| `rental_licence_country` | string | yes | `CALLER` | Cross-border rules differ |
| `rental_licence_expiry` | date | yes | `CALLER` | Must be valid at collection |
| `additional_drivers` | list | yes | `CALLER` | Each needs their own licence |
| `rental_insurance_option` | enum | yes | `CALLER` | excess reduction, etc. |
| `rental_excess_amount` | money | no | `CONFIG` | |
| `rental_deposit_amount` | money | no | `CONFIG` | **Never taken on the call** |
| `rental_pickup_location` | string | conditional | `CALLER` | |
| `rental_return_location` | string | conditional | `CALLER` | |
| `rental_price_per_day` / `_total` | money | no | `DMS` | **Never invented** |
| `rental_agreement_id` | string | no | `DMS` | Set when the desk signs |
| `rental_desk_handoff` | bool | no | `TOOL` | Voice captures; the desk completes |

## 5. Warranty and coverage

| Variable | Type | Ask? | Source | Notes |
|---|---|---|---|---|
| `warranty_active` | bool | no | `DMS` | |
| `warranty_expiry` | date | no | `DMS` | |
| `warranty_provider` | string | conditional | `CALLER` | Third-party warranties often sit outside the DMS |
| `warranty_policy_number` | string | conditional | `CALLER` | High-stakes capture if used |
| `is_warranty_claim` | bool | inferred | `TOOL` | From service type + coverage |
| `claim_reference` | string | no | `DMS` | |
| `claim_requires_assessment` | bool | no | `CONFIG` | Decides whether the agent must escalate |

## 6. Billing, payment and complaints

| Variable | Type | Ask? | Source | Notes |
|---|---|---|---|---|
| `outstanding_balance` | money | no | `DMS` | **Only state if a tool returned it** |
| `invoice_reference` | string | yes | `CALLER` | Capture + read back |
| `invoice_amount` | money | no | `DMS` | |
| `disputed_amount` | money | yes | `CALLER` | |
| `duplicate_invoice_suspected` | bool | yes | `CALLER` | Recurring theme in the data |
| `payment_method_on_file` | enum | no | `DMS` | Never read card numbers aloud |
| `payment_taken_on_call` | bool | no | — | **Always false.** Policy, not limitation |
| `complaint_category` | enum | inferred | `INFER` | billing / quality / wait / no_callback |
| `prior_contact_attempts` | int | no | `DMS` | Drives the apology framing |
| `callback_promised_by` | date | yes | `CALLER` | Sets the expectation explicitly |

## 7. Outcomes and observability

| Variable | Type | Ask? | Source | Notes |
|---|---|---|---|---|
| `primary_intent` | enum | inferred | `INFER` | booking / rental / warranty / billing / complaint / other |
| `secondary_intent` | enum | inferred | `INFER` | Multi-intent calls are common |
| `call_outcome` | enum | inferred | `INFER` | booked / info_only / transferred / unresolved / abandoned |
| `escalated_to_human` | bool | no | `TOOL` | |
| `escalation_reason` | enum | inferred | `INFER` | frustration / request / tool_failure / policy |
| `customer_frustration` | enum | inferred | `INFER` | none / mild / clear / severe |
| `repeated_themselves` | bool | inferred | `INFER` | Early signal of an unresolved loop |
| `agent_failure` | string | inferred | `INFER` | Free text: what went wrong |
| `detail_lost` | string | inferred | `INFER` | **Specifically detects forgetting/contradiction** |
| `plate_read_back` | bool | inferred | `INFER` | Did the guardrail actually fire? |
| `tool_failures` | list | no | `TOOL` | Which integrations failed |
| `next_best_action` | enum | inferred | `INFER` | callback / escalate / send_link / none |

---

## Why these groupings

- **`CALLER` vs `DMS`** is the split that decides where guardrails go. Anything
  `CALLER` needs the capture pipeline; anything `DMS` must never be generated.
- **`INFER` is deliberately small and non-binding.** Intent, frustration and
  urgency are genuinely language judgements. Price, availability and identity are
  not, and are never in this column.
- **`CONFIG` exists so dealer differences are data, not code.** Ten dealers with
  ten different policies should not mean ten flows.

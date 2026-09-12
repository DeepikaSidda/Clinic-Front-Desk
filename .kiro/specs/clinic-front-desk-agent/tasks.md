# Implementation Plan: Clinic Front-Desk Voice Agent

## Overview

This plan implements the two-agent clinic front-desk system in **Python**, using the **Strands Agents SDK** for agent/tool orchestration, **Amazon Nova Sonic (via Amazon Bedrock, boto3)** for the real-time voice layer, **Amazon DynamoDB** for persistence, and **Amazon Bedrock AgentCore Runtime** for deployment. Property-based tests use **Hypothesis**; the DynamoDB contract is exercised against **DynamoDB-local**, while property tests run against **in-memory fake stores**.

The build proceeds bottom-up so dependencies are always satisfied: data models → Data_Layer interfaces + in-memory fakes → DynamoDB implementations → clinic-config domain logic → Strands tool suite → Voice_Front_Desk orchestration + guardrails → Nova Sonic voice integration → Practice_Intelligence + detectors → Dashboard BFF + components → AgentCore deployment wiring. Each of the 28 correctness properties from the design is implemented as a single Hypothesis property-based test (100+ iterations), and the example/edge/integration/smoke/latency tests from the design's Testing Strategy are included alongside the code they cover.

Conventions used in this plan:
- Every property test is tagged with a comment: `Feature: clinic-front-desk-agent, Property N: <text>` and runs a minimum of 100 iterations.
- Test sub-tasks are marked with `*` and are optional (skippable for a faster MVP); core implementation sub-tasks are never optional.
- The agent-facing business logic (tools, Data_Layer, orchestration state machine, detectors, metrics) is the core; the dashboard web UI and voice/AgentCore wiring build on top of it.

## Tasks

- [x] 1. Project setup and tooling
  - [x] 1.1 Set up the Python project structure, dependencies, and test tooling
    - Create the package layout: `src/clinic_front_desk/` with sub-packages `models`, `data_layer` (`interfaces`, `memory`, `dynamodb`, `events`), `config`, `tools`, `voice`, `intelligence`, `dashboard`, and a top-level `tests/` tree (`tests/property`, `tests/unit`, `tests/integration`, `tests/smoke`, `tests/latency`)
    - Configure dependencies: `strands-agents` SDK, `boto3` (Bedrock + DynamoDB), `hypothesis`, `pytest`, and a DynamoDB-local test fixture (e.g. via `amazon-dynamodb-local`/`moto` or a container helper)
    - Add `pyproject.toml`/`requirements.txt`, a `pytest.ini` (register `property`/`integration`/`smoke`/`latency` markers), and a `conftest.py` skeleton
    - _Requirements: 16.1_

- [x] 2. Core data models and shared result types
  - [x] 2.1 Implement domain data models and Result/error types
    - Define dataclasses/typed models: `Provider`, `ScheduleRule`, `ServiceConfig`, `ClinicKnowledgeBase`, `Slot`, `SlotStatus`, `Patient`, `Appointment`, `AppointmentStatus`, `WaitlistEntry`, `Decision`, `DecisionKind`, `DecisionStatus`, `CallSession`, `CallOutcome`, `Escalation`, `EscalationReason`, `Finding`
    - Define the shared `Result[T]` = success/failure union, `StoreError`, and `ToolError` (`store_failure`, `not_found`, `ambiguous`, `validation`, `not_offered`, plus waitlist `duplicate`) discriminated types used across tool and store boundaries
    - Ensure all schedule-owning models (`Slot`, `Appointment`) carry a required `providerId`, and `WaitlistEntry` carries a monotonic `seq` tiebreaker
    - _Requirements: 16.3_

  - [x]* 2.2 Write unit tests for data model construction and (de)serialization
    - Test model construction, enum/status values, and round-trip serialization to/from the DynamoDB item shape
    - _Requirements: 16.3_

- [x] 3. Data_Layer interfaces and in-memory fakes
  - [x] 3.1 Define the seven Data_Layer store interfaces, Result contract, and ChangeEvent
    - Define abstract interfaces `AppointmentStore`, `PatientStore`, `WaitlistStore`, `DecisionStore`, `ClinicKnowledgeBaseStore`, `CallSessionStore`, `EscalationStore` with the exact operation signatures from the design, all returning `Result[T]`
    - Define `ChangeEvent { entity, id, kind }` and a change-emitter hook interface invoked on every successful mutation
    - Document the contract: writes leave prior records unchanged on failure (atomicity) and any Appointment/Slot/schedule write without a `providerId` is rejected
    - _Requirements: 16.1, 16.2, 16.5, 16.6, 16.7_

  - [x] 3.2 Implement in-memory fake stores for all seven interfaces
    - Implement fakes satisfying the same contract, with provider-id enforcement, atomic (all-or-nothing) writes, empty-init behavior, waitlist ascending order with `seq` tiebreak, open-Decisions newest-first ordering, and ChangeEvent emission on success
    - _Requirements: 16.2, 16.3, 16.4, 16.6, 16.7_

  - [x] 3.3 Implement a fault-injection store wrapper for failure-path tests
    - Wrap any store to force a chosen operation to return a failure `Result` (used by atomicity/failure-path properties)
    - _Requirements: 16.6_

  - [x]* 3.4 Write property test for provider-id enforcement
    - **Property 24: Provider-id association and enforcement**
    - **Validates: Requirements 16.3, 16.7**

  - [x]* 3.5 Write property test for empty initialization
    - **Property 25: Empty initialization**
    - **Validates: Requirements 16.4**

  - [x]* 3.6 Write property test for write atomicity across all interfaces
    - **Property 26: Write atomicity across all interfaces** (uses the fault-injection wrapper)
    - **Validates: Requirements 1.6, 2.8, 3.8, 4.9, 5.8, 7.4, 8.5, 9.9, 11.3, 13.8, 16.6**

  - [x]* 3.7 Write property test for appointment→patient referential integrity
    - **Property 7: Every appointment references an existing patient**
    - **Validates: Requirements 3.5**

  - [x]* 3.8 Write property test for waitlist ordering
    - **Property 10: Waitlist ordering is stable and ascending by time added**
    - **Validates: Requirements 7.3**

  - [x]* 3.9 Write property test for open Decisions feed ordering
    - **Property 19: Open Decisions feed ordering** (`DecisionStore.listOpen` newest-first)
    - **Validates: Requirements 14.1**

  - [x]* 3.10 Write smoke test for per-entity interface existence and interface-only access
    - Assert a distinct interface exists per persisted record type and that agent/tool code depends only on the interfaces, never on storage directly
    - _Requirements: 16.1_

- [x] 4. DynamoDB store implementations
  - [x] 4.1 Implement DynamoDB single-table store implementations for all seven interfaces
    - Implement the `PK`/`SK` + GSI single-table layout from the design (ClinicKnowledgeBase, Provider, Slot, Appointment, Patient, WaitlistEntry, Decision, CallSession, Escalation), returning success `Result`s on write and emitting ChangeEvents
    - Enforce provider-id presence, atomic config save (no partial update), and empty-init reads identical to the fakes
    - _Requirements: 16.2_

  - [x]* 4.2 Write integration test for storage-swap observable equivalence
    - **Property 27: Storage-swap observable equivalence** — run the same operation sequences against DynamoDB-local and the in-memory fake and assert equivalent observable results
    - **Validates: Requirements 16.5**

  - [x]* 4.3 Write unit test for single-write persistence success
    - Assert a single write through each interface persists and returns a success result
    - _Requirements: 16.2_

- [x] 5. Clinic configuration domain logic
  - [x] 5.1 Implement clinic-config validation and save orchestration
    - Implement bound checks (services 1–100, prep instructions ≤ 2000 chars, price 0.01–999,999.99, providers 1–50, provider name 1–100 chars), required-field checks (hours, location, ≥1 service, ≥1 provider) that name each missing field and retain entered values, and atomic save through `ClinicKnowledgeBaseStore` with success/failure signaling and no partial update
    - _Requirements: 1.2, 1.4, 1.5, 1.6_

  - [x]* 5.2 Write property test for config validation bounds
    - **Property 1: Clinic config validation respects field bounds**
    - **Validates: Requirements 1.2**

  - [x]* 5.3 Write property test for missing-required-field rejection
    - **Property 2: Missing-required-field rejection is complete and non-destructive**
    - **Validates: Requirements 1.5**

  - [x]* 5.4 Write unit tests for config save success and persistence failure
    - Cover successful save with success indication (1.4) and persistence-failure rejection that retains values without partial update (1.6)
    - _Requirements: 1.4, 1.6_

- [x] 6. Strands tool suite (patient-facing tools)
  - [x] 6.1 Implement the offered-service matcher and `check_availability` tool
    - Implement exact offered-service matching (name equals an offered service → that service; otherwise not-offered, no selection) and `check_availability` returning open slots (default limit 3) with concrete dates/times, plus the retrieval-failure result path
    - _Requirements: 2.1, 2.2, 2.3, 2.9, 2.10, 4.4_

  - [x]* 6.2 Write property test for offered-service matching
    - **Property 3: Offered-service matching**
    - **Validates: Requirements 2.1, 2.9**

  - [x]* 6.3 Write property test for availability offers
    - **Property 4: Availability offers at most three dated slots** (offered count = min(3, open slots); empty → waitlist offer)
    - **Validates: Requirements 2.3, 2.7**

  - [x] 6.4 Implement `book_appointment`, `reschedule`, and `cancel` tools with slot lifecycle
    - Implement booking (write appointment, slot → `booked`, failure leaves no partial appointment), reschedule (move to new slot, old slot → `open`, failure leaves both slots unchanged), and cancel (remove appointment, release slot, failure leaves appointment unchanged)
    - _Requirements: 2.5, 2.6, 2.8, 4.7, 4.8, 4.9, 5.5, 5.7, 5.8_

  - [x]* 6.5 Write property test for booking round-trip and slot lifecycle
    - **Property 5: Booking round-trip and slot lifecycle**
    - **Validates: Requirements 2.5, 2.6, 4.7, 4.8, 5.5, 5.7**

  - [x] 6.6 Implement `lookup_patient` tool and patient creation
    - Retrieve records matching name + callback phone, support extra-identifier disambiguation, create a new patient when none match, and expose the retrieval/persistence failure result paths
    - _Requirements: 3.1, 3.3, 3.4, 3.6, 3.7, 3.8_

  - [x]* 6.7 Write property test for patient lookup and disambiguation
    - **Property 6: Patient lookup round-trip and disambiguation convergence**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6**

  - [x] 6.8 Implement `answer_faq` tool
    - Return answers for topics (hours, location, what_to_bring, prep, insurance, pricing); require a service for pricing; return an unavailable result (never a fabricated answer) for absent topics/services and for tool errors
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x]* 6.9 Write property test for FAQ pricing and availability
    - **Property 9: FAQ pricing and information availability**
    - **Validates: Requirements 6.3, 6.4, 6.5**

  - [x] 6.10 Implement `add_to_waitlist` and `fill_gap_from_waitlist` tools
    - Implement waitlist add with active-duplicate suppression and confirmation fields, and gap-fill that selects the earliest matching waitlisted patient, books them into the slot, and removes their entry, with failure paths that leave slot/entry unchanged and record incompletion
    - _Requirements: 7.1, 7.2, 7.4, 7.5, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [x]* 6.11 Write property test for waitlist add round-trip and no duplicates
    - **Property 11: Waitlist add round-trip and no active duplicates**
    - **Validates: Requirements 7.1, 7.2, 7.5**

  - [x] 6.13 Implement `flag_for_human` tool and escalation recording
    - Persist an escalation carrying a reason from {clinical_content, outside_admin_rules, patient_distress, patient_request}, the call-session context, and patient identity when known; expose the escalation-failure result path
    - _Requirements: 9.4, 9.9_

  - [x]* 6.15 Write unit tests for FAQ and waitlist clarifying behaviors
    - Cover single-answer FAQ (6.2), ambiguous-FAQ clarification (6.7), and decline-waitlist (7.6)
    - _Requirements: 6.2, 6.7, 7.6_

- [x] 7. Voice_Front_Desk orchestration and guardrails
  - [x] 7.1 Implement `SessionContext` (fact retention and task step index)
    - Retain identifying details, requested service, requested date/time, and slot selection for the session; expose the current task step index for barge-in resume; persist outcome on session end
    - _Requirements: 11.1, 11.4_

  - [x]* 7.2 Write property test for session context retention and outcome persistence
    - **Property 15: Session context retention and outcome persistence**
    - **Validates: Requirements 11.1, 11.4, 11.5, 12.7**

  - [x] 7.3 Implement `GuardrailPolicy` and the administrative-only system prompt
    - Classify each turn (administrative vs clinical/symptom/out-of-rules); implement the prompt-layer guardrail (system prompt forbids clinical advice/triage/diagnosis/treatment/medication and routes only by patient-named service) and the tool-layer guardrail (no symptom→service path; call `flag_for_human` when no patient-named offered service can be obtained)
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [x]* 7.4 Write property test for symptom-never-infers-service guardrail
    - **Property 14: Symptom inputs never infer a service**
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.6**

  - [x] 7.5 Implement `ToolOrchestrator` with tool chaining and confirmation semantics
    - Chain tools so each output feeds the next; on mid-chain failure retain gathered context and offer to take a message; enforce confirm-before-mutate for reschedule/cancel (declined cancellation and no-alternative reschedule leave state unchanged)
    - _Requirements: 11.2, 11.3, 4.5, 5.6_

  - [x]* 7.6 Write property test for reschedule/cancel confirmation semantics
    - **Property 8: Reschedule/cancel confirmation semantics preserve state**
    - **Validates: Requirements 4.5, 5.6**

  - [x] 7.7 Implement `TurnController` (interpretation failures, silence, voice-layer loss)
    - Bound consecutive interpretation-failure re-asks to 2 then escalate via `flag_for_human`; re-prompt once after 10 s silence; on voice-layer loss inform patient, end session, record outcome `interrupted`
    - _Requirements: 12.4, 12.5, 12.6, 12.7_

  - [x]* 7.8 Write property test for bounded interpretation-failure retries
    - **Property 16: Interpretation-failure retries are bounded then escalate**
    - **Validates: Requirements 12.4, 12.5**

  - [x] 7.9 Implement barge-in resume logic in the orchestration/session
    - On barge-in, preserve accumulated context and the current step index so the task resumes from its pre-interruption step after the interruption is processed
    - _Requirements: 12.3_

  - [x]* 7.10 Write property test for barge-in preserve-and-resume
    - **Property 17: Barge-in preserves and resumes task step**
    - **Validates: Requirements 12.3**

  - [x] 7.11 Implement Call_Session outcome persistence and empty-config voice behavior
    - Persist the call outcome (booked/rescheduled/cancelled/waitlisted/escalated/no_action/interrupted) with patient-provided identity on session end; while no hours and no services are configured, respond that the clinic is not yet accepting calls and offer to take a message
    - _Requirements: 1.7, 11.5, 12.7_

  - [x]* 7.12 Write unit tests for orchestration clarifying/decline behaviors
    - Cover empty-config response (1.7), no-appointment-offer-to-book (4.2), cancel confirmation prompt (5.4), distress escalation offer (9.3), and human-follow-up message (9.5)
    - _Requirements: 1.7, 4.2, 5.4, 9.3, 9.5_

  - [x]* 7.13 Write property test for escalation classification and recording
    - **Property 13: Escalation classification and faithful recording**
    - **Validates: Requirements 9.1, 9.2, 9.4, 9.7, 9.8, 10.5**

- [x] 8. Checkpoint - core business logic
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Voice / Nova Sonic integration
  - [x] 9.1 Implement `VoiceStreamManager` over the Strands `BidiAgent` + Nova Sonic
    - Wrap the bidirectional Nova Sonic speech-to-speech stream (via Bedrock): manage stream lifecycle, emit interpreted turns, drive response-start timing and barge-in stop; provide a mockable boundary for orchestration tests
    - _Requirements: 12.1, 12.2_

  - [x] 9.2 Wire the Voice_Front_Desk agent end to end
    - Register the nine patient-facing tools with the `BidiAgent`, attach the guardrail system prompt, and connect `SessionContext`/`ToolOrchestrator`/`TurnController`/barge-in to the voice stream
    - _Requirements: 2.4, 3.2, 11.2, 12.3_

  - [x]* 9.3 Write latency test for response-start
    - Measure response-start ≤ 1.5 s against a Nova Sonic test stream
    - _Requirements: 12.1_

  - [x]* 9.4 Write latency test for barge-in stop
    - Measure barge-in stop ≤ 500 ms against a Nova Sonic test stream
    - _Requirements: 12.2_

- [x] 10. Practice_Intelligence and detectors
  - [x] 10.1 Implement `analyze_patterns` tool and `PatternDetectors`
    - Implement detectors (no-show trend, schedule gap/utilization, unmet demand, unoffered-service demand, and open-slot↔waitlist gap-fill matching) returning `Finding`s with supporting-record counts and stable `findingKey`s; read over Appointment/Waitlist/CallSession data
    - _Requirements: 13.2, 8.1_

  - [x] 10.2 Implement `DecisionSynthesizer` with generation gates and persistence
    - Generate a Decision iff the finding is actionable, has ≥ 5 supporting records, and no open Decision shares its `findingKey`; persist Decisions; on analysis failure generate no Decision and record the failure; on persistence failure retain the finding for the next run with no duplicate
    - _Requirements: 13.3, 13.4, 13.5, 13.6, 13.7, 13.8_

  - [x] 10.3 Implement `AnalysisScheduler`
    - Fire analysis runs on a recurring interval not exceeding 24 h
    - _Requirements: 13.1_

  - [x]* 10.4 Write property test for decision generation gates
    - **Property 18: Decision generation gates (threshold, actionability, dedup)**
    - **Validates: Requirements 13.3, 13.4, 13.6**

  - [x]* 10.5 Write property test for analysis-failure producing no decisions
    - **Property 28: Analysis-failure produces no Decisions**
    - **Validates: Requirements 13.7**

  - [x]* 10.6 Write property test for gap-fill generation, selection, and assignment
    - **Property 12: Gap-fill generation, earliest-selection, and assignment**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.6**

  - [x]* 10.7 Write smoke test for scheduler interval
    - Assert the analysis interval is ≤ 24 h
    - _Requirements: 13.1_

  - [x]* 10.8 Write unit test for analyze_patterns invocation
    - Assert an analysis run invokes `analyze_patterns` over Appointment/Waitlist/CallSession data
    - _Requirements: 13.2_

- [x] 11. Checkpoint - autonomous intelligence
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Dashboard backend-for-frontend and real-time channel
  - [x] 12.1 Implement the Dashboard BFF reading through the Data_Layer with change-event fan-out
    - Build the thin BFF that reads exclusively through Data_Layer interfaces and fans ChangeEvents out to connected clients over a WebSocket/SSE channel (decision add ≤ 5 s, removal ≤ 2 s, schedule/activity ≤ 5 s, escalation ≤ 5 s propagation budgets)
    - _Requirements: 14.8, 14.5, 15.4, 9.6_

  - [x] 12.2 Implement decision approve/dismiss execution flow
    - On approve: record `approved` and execute the associated action through the Data_Layer (including gap-fill), removing from the feed on success; on action-persistence failure keep the Decision open (action-failed) with an error indication and no partial effect; on dismiss: record `dismissed` and execute no action
    - _Requirements: 14.3, 14.4, 14.5, 14.6, 8.2, 8.3, 8.4, 8.5_

  - [x]* 12.3 Write property test for decision resolution outcomes
    - **Property 20: Decision resolution outcomes**
    - **Validates: Requirements 14.3, 14.4, 14.6**

  - [x] 12.4 Implement `RoleGate` access control
    - Present only the views permitted for an assigned role; deny access and return no schedule/activity/metrics data for a viewer without an assigned role
    - _Requirements: 15.5, 15.7_

  - [x]* 12.5 Write property test for role-scoped access
    - **Property 23: Role-scoped access**
    - **Validates: Requirements 15.5, 15.7**

  - [x] 12.6 Implement the impact-metrics service
    - Compute front-desk hours saved, waitlist-recovered appointment count, and no-show rate over 7/30/90-day windows, with the no-show-rate trend = current-period rate minus the immediately preceding equal-length period
    - _Requirements: 15.3_

  - [x]* 12.7 Write property test for impact metrics computation and trend
    - **Property 21: Impact metrics computation and trend**
    - **Validates: Requirements 15.3**

  - [x] 12.8 Implement the call-activity-log aggregation
    - Aggregate call sessions and escalations into a most-recent-first log where each entry exposes interaction type (booked/rescheduled/cancelled/escalated), date-time, and patient identifier
    - _Requirements: 15.2, 9.6_

  - [x]* 12.9 Write property test for activity log content and ordering
    - **Property 22: Activity log content and ordering**
    - **Validates: Requirements 15.2**

- [x] 13. Dashboard web components
  - [x] 13.1 Implement the `DecisionsFeed` component
    - Render open Decisions newest-first with approve/dismiss controls, optimistic removal reconciled on ChangeEvent, real-time add, and an empty-state message
    - _Requirements: 14.1, 14.2, 14.5, 14.7, 14.8_

  - [x] 13.2 Implement the `ScheduleView` component
    - Show the provider's appointments and open slots for the current day by default, reflect changes within 5 s, and load a selected non-current day within 2 s
    - _Requirements: 15.1, 15.4, 15.6_

  - [x] 13.3 Implement the `CallActivityLog` and `ImpactMetricsStrip` components
    - Render the activity log and the metrics strip (hours saved, waitlist-recovered count, no-show rate + trend) with a 7/30/90-day period selector, reflecting changes within 5 s
    - _Requirements: 15.2, 15.3, 15.4_

  - [x] 13.4 Implement the `OnboardingWizard` component
    - Present the onboarding workflow on first access when no config exists, wired to the config validation/save logic, showing per-field errors and retaining entered values
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 13.5 Implement the `RoleGate` UI enforcement
    - Enforce role-scoped view presentation and no-role access denial in the client
    - _Requirements: 15.5, 15.7_

  - [x]* 13.6 Write unit tests for dashboard rendering states
    - Cover decision controls rendering (14.2), empty Decisions feed (14.7), and default schedule view (15.1)
    - _Requirements: 14.2, 14.7, 15.1_

- [x] 14. Integration and AgentCore deployment wiring
  - [x] 14.1 Wire both agents, the Data_Layer, and the Dashboard BFF for AgentCore Runtime
    - Compose the Voice_Front_Desk (bidirectional WebSocket transport), the scheduled Practice_Intelligence entrypoint, the shared Data_Layer library, and the Dashboard BFF into AgentCore Runtime deployment entrypoints/config; apply live config-update propagation (updates reflected in responses beginning within 5 s, no restart)
    - Deployable artifacts: `deployment/server.py` implements the AgentCore Runtime HTTP protocol contract (`GET /ping` Healthy/HealthyBusy, `POST /invocations` for the scheduled analysis run plus role-gated BFF reads and decision approve/dismiss, `WebSocket /ws` for the voice transport) on one ARM64 container port 8080; `entrypoint.py`, `Dockerfile`, `.bedrock_agentcore.yaml`, and `deploy/` (IAM trust + execution-role policies, EventBridge schedule for the ≤ 24 h cadence, deploy guide)
    - _Requirements: 1.8, 16.1_

  - [x]* 14.2 Write integration tests for config-change and escalation propagation
    - Cover config-change propagation ≤ 5 s (1.8) and escalation surfacing in the activity log ≤ 5 s (9.6)
    - _Requirements: 1.8, 9.6_

  - [x]* 14.3 Write integration tests for decision feed propagation
    - Cover decision feed add ≤ 5 s (14.8) and decision removal ≤ 2 s (14.5)
    - _Requirements: 14.8, 14.5_

  - [x]* 14.4 Write integration tests for schedule and activity propagation
    - Cover schedule/activity reflection ≤ 5 s (15.4) and non-current-day schedule fetch ≤ 2 s (15.6)
    - _Requirements: 15.4, 15.6_

- [x] 15. Final checkpoint - ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; the 28 property tests, however, are the primary correctness guarantee and should be prioritized.
- Each property test is implemented as a single Hypothesis test, runs ≥ 100 iterations, and is tagged `Feature: clinic-front-desk-agent, Property N: <text>`.
- Property tests run against in-memory fake stores; Property 27 additionally runs against DynamoDB-local for storage-swap equivalence.
- Latency behaviors (12.1, 12.2) are measured against a Nova Sonic test stream, not property-tested, since they are timing characteristics of the voice pipeline.
- Every task references the specific requirement clauses (and, where applicable, the design property number) it implements, for traceability.
- The 28 correctness properties map 1:1 to tasks: P1→5.2, P2→5.3, P3→6.2, P4→6.3, P5→6.5, P6→6.7, P7→3.7, P8→7.6, P9→6.9, P10→3.8, P11→6.11, P12→10.6, P13→7.13, P14→7.4, P15→7.2, P16→7.8, P17→7.10, P18→10.4, P19→3.9, P20→12.3, P21→12.7, P22→12.9, P23→12.5, P24→3.4, P25→3.5, P26→3.6, P27→4.2, P28→10.5.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3"] },
    { "id": 4, "tasks": ["3.4", "3.5", "3.6", "3.7", "3.8", "3.9", "3.10", "4.1", "5.1"] },
    { "id": 5, "tasks": ["4.2", "4.3", "5.2", "5.3", "5.4", "6.1", "6.4", "6.6", "6.8", "6.10", "6.13", "10.1"] },
    { "id": 6, "tasks": ["6.2", "6.3", "6.5", "6.7", "6.9", "6.11", "6.15", "7.1", "7.3", "7.7", "10.2", "10.3"] },
    { "id": 7, "tasks": ["7.2", "7.4", "7.5", "7.8", "7.9", "7.11", "7.13", "10.4", "10.5", "10.6", "10.7", "10.8"] },
    { "id": 8, "tasks": ["7.6", "7.10", "7.12", "9.1", "9.2", "12.1"] },
    { "id": 9, "tasks": ["9.3", "9.4", "12.2", "12.4", "12.6", "12.8"] },
    { "id": 10, "tasks": ["12.3", "12.5", "12.7", "12.9", "13.1", "13.2", "13.3", "13.4", "13.5"] },
    { "id": 11, "tasks": ["13.6", "14.1"] },
    { "id": 12, "tasks": ["14.2", "14.3", "14.4"] }
  ]
}
```

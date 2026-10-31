# Bugfix Requirements Document

## Introduction

The ULTRON agent module fails to connect to the Gemini API due to SSL handshake timeout errors, preventing dynamic mission plan generation. All LLM planning calls fail with `_ssl.c:1064: The handshake operation timed out`, forcing the system to operate in degraded mode using only cached/pre-recorded plans. This bugfix addresses the connection reliability, diagnostic visibility, and error recovery mechanisms to ensure the ULTRON agent can successfully generate mission-specific plans and respond dynamically to changing mission conditions.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the ULTRON agent attempts to call the Gemini API for initial mission planning THEN the system fails with an SSL handshake timeout error (`_ssl.c:1064: The handshake operation timed out`)

1.2 WHEN the ULTRON agent attempts to call the Gemini API for replanning (triggered by hazards/targets) THEN the system fails with generic "timed out" errors

1.3 WHEN Gemini API calls fail due to SSL/network issues THEN the system provides no diagnostic information to identify the specific failure point (DNS, SSL handshake, network connectivity, API authentication)

1.4 WHEN the Gemini API key is invalid, revoked, or rate-limited THEN the system attempts connections without validating the API key first, wasting time on doomed connection attempts

1.5 WHEN transient network issues cause connection failures THEN the system does not retry with exponential backoff, treating temporary failures as permanent

1.6 WHEN SSL handshake phase times out THEN the entire request fails immediately without distinguishing between handshake timeout and overall request timeout

1.7 WHEN developers need to troubleshoot API connectivity issues THEN there is no diagnostic CLI command to test connection health independently of mission execution

### Expected Behavior (Correct)

2.1 WHEN the ULTRON agent attempts to call the Gemini API for initial mission planning THEN the system SHALL successfully establish an SSL connection and generate dynamic mission-specific plans

2.2 WHEN the ULTRON agent attempts to call the Gemini API for replanning THEN the system SHALL successfully establish connections and generate updated plans in response to environmental triggers

2.3 WHEN Gemini API calls fail due to SSL/network issues THEN the system SHALL provide clear diagnostic messages identifying the specific failure point (DNS resolution failure, SSL handshake timeout, network unreachable, HTTP error code, authentication failure) with actionable troubleshooting steps

2.4 WHEN the Gemini API key is configured THEN the system SHALL validate the API key before attempting mission-critical planning calls, providing immediate feedback if the key is invalid or rate-limited

2.5 WHEN transient network issues cause connection failures THEN the system SHALL implement retry logic with exponential backoff (e.g., 3 attempts with 1s, 2s, 4s delays) to recover from temporary failures

2.6 WHEN establishing SSL connections to the Gemini API THEN the system SHALL configure separate timeout values for SSL handshake phase (e.g., 45s) and overall request timeout (30s for planning, 20s for replanning) to accommodate slower SSL negotiation on constrained networks

2.7 WHEN developers need to troubleshoot API connectivity issues THEN the system SHALL provide a CLI diagnostic command (`python ultron/main.py test-connection` or similar) that validates DNS resolution, SSL handshake, API key authentication, and basic API call functionality

### Unchanged Behavior (Regression Prevention)

3.1 WHEN Gemini API calls fail after all retry attempts are exhausted THEN the system SHALL CONTINUE TO fall back to cached/pre-recorded plans successfully

3.2 WHEN the ULTRON agent operates using cached plans (due to API unavailability) THEN the system SHALL CONTINUE TO complete missions successfully in degraded mode

3.3 WHEN mission objectives, vehicle states, and environmental triggers are processed THEN the system SHALL CONTINUE TO use the same data structures and interfaces for plan generation

3.4 WHEN the system is configured with `GEMINI_API_KEY` environment variable THEN it SHALL CONTINUE TO read the API key from `.env` using the existing configuration mechanism

3.5 WHEN timeout values are configured for planning (30s) and replanning (20s) THEN the system SHALL CONTINUE TO respect overall request timeout limits after successful SSL handshake

3.6 WHEN the ULTRON agent generates plans (whether via API or cached fallback) THEN the downstream mission execution components SHALL CONTINUE TO consume plans without modification

"""The narrow/broad gate: only questions about one part of a document are pruned."""

from __future__ import annotations

import pytest

from horizon.transforms.query_prune import question_scope

BROAD = [
    "Review this document and generate a plan for me on how to build it.",
    "Review this product spec and list the biggest gaps, risks and open questions before engineering starts.",
    "Summarize this spec and give me an implementation checklist.",
    "Review this plan and tell me what to build first and why.",
    "Read this README and explain how to set the project up and what its main limitations are.",
    "Summarize the decisions in this document and flag any that conflict or look risky.",
    "What do you think of this design?",
    "Can you proofread this?",
    "tl;dr please",
    "Explain this document to me.",
    "Find every place where the spec contradicts itself.",
    "Rewrite this in plain English.",
    "Is anything missing from this proposal?",
    "Go through it and tell me what's wrong.",
    "Compare the two approaches described here.",
    "What are the key takeaways?",
    "Turn this into a checklist of action items.",
    "Analyze this contract for risks.",
    "Translate this to Spanish.",
    "How should I build this?",  # whole-document intent: 'build this'
    # Held out while the rules were written, added afterwards as regressions.
    'Can you look over this and tell me if it makes sense?',
    'Help me understand this spec.',
    'I need a high-level breakdown of this.',
    'Are there any security problems in this design?',
    'Write tests based on this spec.',
    'Turn this into Jira tickets.',
    'Is this ready to ship?',
    'Make this shorter.',
    'Fix the typos.',
    "What's this about?",
    'Estimate how long this would take to build.',
    'Can you score this proposal out of 10?',
]
NARROW = [
    "From this document, what credential and secrets handling does the bot require, and how should API keys be stored and scoped?",
    "How does this document say model artifacts get from the PC training node to the Pi live node?",
    "According to this spec, how should prompt caching orchestration work in Phase A?",
    "What does this spec decide about semantic caching on pgvector (Phase C-1), including thresholds?",
    "How does this spec define rate limiting, and what are the limits per key and per tenant?",
    "What does this plan say about hosting costs and when to move to a real server?",
    "What is Phase 4, task-aware routing, in this plan, and how is it supposed to work?",
    "According to this README, how do I upgrade an existing Postgres volume for Phase C-1?",
    "What runtime settings and environment variables does this README document?",
    "What was decided about the T3 schema floor, and why?",
    "Which port does the dashboard listen on?",
    "When does the weekly S3 export run?",
    "Does the API support streaming responses?",
    "Who owns the database migration step?",
    "Tell me the timeout for the Codex WebSocket.",
    'What retry policy does the uploader use?',
    'How is the API key rotated?',
    'Which database does the ledger use?',
    'Where are model weights stored?',
    "What's the max position size in the risk section?",
    'How many shadow cycles before promotion?',
    'Does section 5 mention a cross-venue check?',
    'What happens when the exchange connection drops?',
    'Is Redis required for the rate limiter?',
    'What is the default value of QUOTA_FAIL_MODE?',
    'Why did they reject text-embedding-3-large?',
    'When is the semantic cache bypassed?',
]
UNCLEAR = ["", "thanks", "?", "Here you go.", "see attached"]


@pytest.mark.parametrize("q", BROAD)
def test_whole_document_requests_are_never_pruned(q):
    # 'broad' and None (unclear) both leave the document whole.
    assert question_scope(q) != "narrow"


@pytest.mark.parametrize("q", NARROW)
def test_questions_about_one_part_are_narrow(q):
    assert question_scope(q) == "narrow"


@pytest.mark.parametrize("q", UNCLEAR)
def test_unclear_prompts_are_left_alone(q):
    assert question_scope(q) is None

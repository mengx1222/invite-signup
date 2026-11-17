"""Per-site adapters.

An adapter holds the hand-verified facts about one site: the endpoints, the
selectors, the verification shape, the referral quota.  The generic prober
discovers most of this automatically; an adapter pins down the parts that
only a human can confirm by actually running the flow.
"""

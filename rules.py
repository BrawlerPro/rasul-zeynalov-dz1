from datetime import datetime

TRANSITIONS = {'new': {'in_progress'}, 'in_progress': {'closed'}, 'closed': {'in_progress'}}

def can_transition(before, after):
    return after in TRANSITIONS.get(before, set())

def reaction_hours(created: datetime, first_response: datetime | None, now: datetime):
    return max(0.0, ((first_response or now) - created).total_seconds() / 3600)

def sla_breached(created, first_response, now, sla_hours):
    return reaction_hours(created, first_response, now) > sla_hours

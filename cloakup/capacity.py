"""Read-only official license capacity, separated from local scheduler counts."""
import time

def license_capacity():
    from cloakbrowser.license import resolve_license_key, get_session_seats, validate_license
    key=resolve_license_key()
    if not key:return {'state':'unconfigured','checked_at':time.time(),'limit':None,'active':None}
    seats=get_session_seats(key)
    info=validate_license(key)
    return {'state':seats.state,'limit':seats.limit,'active':seats.active,'plan':info.plan if info else None,
            'checked_at':time.time(),'available':max(0,seats.limit-seats.active) if seats.limit is not None and seats.active is not None else None}

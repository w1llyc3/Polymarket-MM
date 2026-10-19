"""Compact observed-price index; original frames remain in SQLite."""
from ..fair_value.maths import decimal
from .reference import book_data_risk


def price_point(frame, policy):
    at=frame['time_ms'];book=frame.get('book') or {}
    good=not book_data_risk({**frame,'phase':'ACTIVE_MM'},policy,at)
    limits=[]
    for value,age in ((frame.get('book_received_ms'),policy.book_max_age_ms),
                      (frame.get('market_observed_ms'),policy.metadata_max_age_ms),
                      (at,policy.book_max_age_ms)):
        limits.append(value+age if type(value) is int else at-1)
    return {'time_ms':at,'mid':str((decimal(book['bid'])+decimal(book['ask']))/2) if good else None,
        'bid':book.get('bid'),'ask':book.get('ask'),'valid_until_ms':min(limits),
        'book_ref':frame.get('book_ref'),'book_received_ms':frame.get('book_received_ms')}

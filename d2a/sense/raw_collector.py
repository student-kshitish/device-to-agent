"""
d2a/sense/raw_collector.py — RawCollector: call sources, merge readings.

Each source's read() is invoked FRESH right now. A source returning None is
recorded as {"_unavailable": True} so downstream stages can lower confidence
without crashing. Exceptions are also caught and converted to unavailable.
"""


class RawCollector:
    """
    Calls every SignalSource in the given list exactly once, right now.
    Merges into {source.name: data_dict_or_sentinel}.
    Never raises — a broken source produces {"_unavailable": True} at its key.
    """

    def collect(self, sources: list) -> dict:
        """
        Read all sources.
        Returns {source.name: dict_from_read_or_{"_unavailable": True}}.
        Each entry carries its own raw values exactly as the source produced them.
        """
        result: dict = {}
        for source in sources:
            try:
                val = source.read()
                result[source.name] = val if val is not None else {"_unavailable": True}
            except Exception:
                result[source.name] = {"_unavailable": True}
        return result

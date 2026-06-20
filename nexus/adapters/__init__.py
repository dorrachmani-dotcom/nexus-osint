"""Plug & Play adapters that wrap existing OSINT CLI tools.

Rather than reimplementing well-known tooling, each adapter shells out to an
installed binary (Sherlock, Maigret, theHarvester, SpiderFoot), parses its
output, and maps the findings onto RawItems / graph entities. An adapter whose
binary is not on PATH simply reports itself unavailable and is skipped.
"""

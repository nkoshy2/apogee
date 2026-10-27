"""Discovery: fetch public postings from employer job boards into a versioned feed.

Everything under aims.discovery except importer.py runs in the cloud (GitHub Actions), so it
must use only the standard library and must never import the database, settings, or any
other private data (a test enforces this). importer.py is the local side that reads a feed
into the database.
"""

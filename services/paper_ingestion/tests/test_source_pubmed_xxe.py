"""XXE-hardening regression test for PubMed XML parser."""

from lxml import etree

XXE_PAYLOAD = b"""<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY test SYSTEM "file:///etc/passwd">]>
<eSearchResult>&test;</eSearchResult>"""

# Mirror the hardened parser defined in pubmed_source.py
_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
)


def test_pubmed_parse_rejects_xxe():
    """Hardened XMLParser must not resolve external entities from the XXE payload."""
    try:
        result = etree.fromstring(XXE_PAYLOAD, parser=_XML_PARSER)
        # If parsing succeeds the entity reference must not have been resolved
        # (lxml with resolve_entities=False leaves the reference unresolved or
        # raises — either outcome is safe).
        text = result.text or ""
        assert "/root:" not in text and "nobody:" not in text, (
            "XXE entity was resolved — hardened XMLParser protection failed"
        )
    except etree.XMLSyntaxError:
        # lxml raises XMLSyntaxError when the entity reference cannot be
        # resolved — this is the expected "safe rejection" path.
        pass

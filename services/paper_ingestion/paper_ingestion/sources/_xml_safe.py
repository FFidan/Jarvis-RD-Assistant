"""XXE-safe XML parsing helpers built on lxml.

Exposes a pre-configured parser that disables external entity resolution,
DTD loading, and network fetches. All source plugins should parse via
`safe_fromstring` — never construct an etree parser inline.
"""

import lxml.etree as etree

_SAFE_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
)


def safe_fromstring(data: bytes | str) -> "etree._Element":
    """Parse an XML document from bytes/str with XXE protections on."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return etree.fromstring(data, parser=_SAFE_PARSER)

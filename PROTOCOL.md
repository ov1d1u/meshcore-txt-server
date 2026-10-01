# Teletext wire protocol (T1)

The Android app and Python server each keep a copy of this document so either directory can become its own repository. Keep both copies and the protocol fixtures in sync when changing the wire format.

All application frames are UTF-8 direct-message text of at most **100 bytes**, including headers. A request ID is four hexadecimal digits. Sequences and counts are unsigned base-36 numbers, starting at zero.

| Frame | Meaning |
| --- | --- |
| `T1I<id>` | GET_INDEX (supported for older clients; same content as page 100) |
| `T1P<id><nnn>` | GET_PAGE, with three-digit page number |
| `T1C<id>` | Cancel that transfer |
| `T1R<id>.<seq>` | Request one missing chunk again, using the original request ID and zero-based sequence |
| `T1D<id>.0.<count>:<text>` | First Markdown data chunk, including the total chunk count |
| `T1D<id>.<seq>:<text>` | Later Markdown data chunk (`seq` is at least 1) |
| `T1Z<id>.0.<count>:<payload>` | First compressed Markdown chunk, including the total chunk count |
| `T1Z<id>.<seq>:<payload>` | Later compressed Markdown chunk (`seq` is at least 1) |
| `T1E<id>.<count>.<crc32>` | End of transfer; count is total data chunks and CRC-32 is eight hex digits |
| `T1X<id>.<code>` | Error: `BAD`, `NF`, `BUSY`, or `IO` |

For example, `T1P7A3F100` requests the index, and `T1P7A3F101` requests page 101. A `Z` payload is one independent zlib stream of UTF-8 Markdown, encoded as unpadded Base64URL text. It expands to at most 1,024 bytes. The server sends `Z` only when it carries more original text than a `D` frame; a transfer may mix both kinds. The CRC-32 covers the original, uncompressed UTF-8 bytes.

The receiver groups responses by sender and request ID, stores decoded chunks by sequence, and shows received versus total chunks once chunk 0 arrives. A divider with a question mark marks missing chunks; it disappears if they arrive later. The verified page replaces the preview after the `E` frame, all chunks, and the CRC-32 match. If retrieval ultimately fails, the incomplete preview remains visible behind the Retry dialog. An empty page has no data frames and ends with count zero and CRC `00000000`. After the end frame, Android requests missing chunks individually; after a stall it can also request a known missing chunk. The server resends it from the original file snapshot, including the total count if chunk 0 is requested. It retains that snapshot until another transfer starts or the request is cancelled. If repair does not complete, Android retries the whole request after 180 seconds without progress, for three attempts total; each attempt uses a new ID. The server paces data frames by MeshCore delivery acknowledgments and tries each frame twice before failing. Update the server and Android app together to use compressed frames.

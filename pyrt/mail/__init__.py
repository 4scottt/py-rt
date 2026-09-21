"""Mail: the outgoing sender and the ticket notifications (plan §6, §9 M06-M08).

``send`` holds the :class:`~pyrt.mail.send.Sender` implementations and the
process-wide accessor the app configures once; ``notify`` holds the three
notifications of FP M07 as hook functions the tickets package calls after a
transaction is written. The inbound side (``parse``, ``gateway``) lands with
M5 and reads :data:`pyrt.mail.notify.TAG_RE` for the subject tag's shape.
"""

from __future__ import annotations

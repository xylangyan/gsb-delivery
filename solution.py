"""Compatibility entry point for evaluators that import ``solution``.

The implementation lives in :mod:`pygments.lexers.sql_jinja_audit`; this module
re-exports the audit API under stable names so both ``import solution`` and the
Pygments package entry points work.
"""

from pygments.lexers.sql_jinja_audit import *  # noqa: F401,F403
from pygments.lexers.templates import SqlJinjaLexer  # noqa: F401
from pygments.lexers.sql_jinja_audit import (  # noqa: F401
    AuditDiff,
    AuditHistory,
    AuditResult,
    AuditToken,
    Correction,
    FragmentMap,
    FragmentSpec,
    ItemChange,
    LiteralItem,
    SegmentIndex,
    SourceInterval,
    SourceSpan,
    SpanRef,
    TemplateReference,
    TokenChange,
    WindowSpec,
    apply_correction,
    apply_correction_window,
    apply_corrections,
    audit,
    audit_fragments,
    audit_history,
    audit_sql_jinja,
    audit_sql_template,
    audit_window,
    build_audit,
    compare_audits,
    compare_sources,
    compare_tokens,
    correction_sheet,
    diff_audits,
    lex_sql_jinja,
    lex_sql_template,
    lex_window,
    literal_items,
    literals,
    reclassified_tokens,
    sequence_items,
    tokenize,
    tokenize_with_spans,
)


SqlJinjaAudit = AuditResult
SqlJinjaIndex = SegmentIndex
SqlJinjaAuditToken = AuditToken
SqlJinjaLiteralItem = LiteralItem


class SqlJinjaAuditor:
    """Small object facade around the module level audit functions."""

    def __init__(self, lexer=None):
        self.lexer = lexer or SqlJinjaLexer(stripnl=False, ensurenl=False)

    def audit(self, text, **options):
        return audit(text, lexer=self.lexer, **options)

    def tokenize(self, text, **options):
        return self.audit(text, **options).tokens

    def items(self, text, **options):
        return self.audit(text, **options).items

    def compare(self, old, new, **options):
        return compare_audits(self.audit(old), self.audit(new), **options)

    def history(self, rounds):
        return audit_history(rounds)


audit_source = audit
audit_draft = audit
audit_slice = audit_window
audit_round = audit_fragments
build_history = audit_history
compare_drafts = compare_sources
diff_drafts = compare_sources
compare_versions = compare_sources
fixed_literals = literal_items
items = literal_items
segments = audit
tokens = audit

solve = audit
run = audit
main = audit


def calculate(text, **options):
    return audit(text, **options)

"""Aggregate the five cohesive paper sub-routers into one application router.

The monolithic papers.py has been split into:
  papers_feed.py       — list_papers_brief, list_papers, get_feed_counts
  papers_detail.py     — get_paper_detail, batch_save_papers
  papers_feedback.py   — submit_feedback, delete_paper_feedback, trash_and_reject_paper
  papers_lifecycle.py  — save/unsave/skip/reading/done/star/unstar/trash/restore/annotate/hard_del
  papers_bulk.py       — bulk_action_papers, process_batch

The split is by responsibility (feed listing, detail fetch, feedback, lifecycle
transitions, bulk actions), not an arbitrary file-size cut; recombining the
sub-routers into one module would re-create the monolith this replaced.
"""

from fastapi import APIRouter

from paper_ingestion.routers.papers_bulk import router as _bulk_router
from paper_ingestion.routers.papers_detail import router as _detail_router
from paper_ingestion.routers.papers_feedback import router as _feedback_router
from paper_ingestion.routers.papers_feed import router as _feed_router
from paper_ingestion.routers.papers_lifecycle import router as _lifecycle_router

# Aggregate all sub-routers into a single router so main.py needs only one
# include_router call and existing callers of ``papers.router`` keep working.
router = APIRouter()
router.include_router(_feed_router)
router.include_router(_detail_router)
router.include_router(_feedback_router)
router.include_router(_lifecycle_router)
router.include_router(_bulk_router)

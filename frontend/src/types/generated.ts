/**
 * Bridge file for OpenAPI-generated types.
 *
 * TODO: Run `npm run codegen` with backend services (Docker) running to
 * generate the actual type files from the OpenAPI specs:
 *
 *   npm run codegen:paper    -> src/types/paper-ingestion.gen.ts  (from :8000/openapi.json)
 *   npm run codegen:learning -> src/types/learning-engine.gen.ts  (from :8001/openapi.json)
 *   npm run codegen           -> both of the above
 *
 * Once generated, uncomment the re-exports below and verify they align with
 * the hand-written types in ./index.ts.  Any mismatch means either the
 * backend model changed or the hand-written type drifted.
 *
 * Migration plan:
 *   1. Run codegen (requires Docker services).
 *   2. Uncomment the type aliases below.
 *   3. Compare generated types against ./index.ts using tsc --noEmit.
 *   4. Replace hand-written interfaces with generated aliases where they match.
 *   5. Keep hand-written types only for frontend-only constructs (e.g. priorityLevel helper).
 */

// ---------------------------------------------------------------------------
// paper_ingestion service types (from paper-ingestion.gen.ts)
// ---------------------------------------------------------------------------
// import type { components as PaperComponents } from './paper-ingestion.gen';
//
// Paper Ingestion — response schemas
// export type Gen_PaperResponse           = PaperComponents['schemas']['PaperResponse'];
// export type Gen_SummaryResponse         = PaperComponents['schemas']['SummaryResponse'];
// export type Gen_ChunkResponse           = PaperComponents['schemas']['ChunkResponse'];
// export type Gen_PaperDetailResponse     = PaperComponents['schemas']['PaperDetailResponse'];
// export type Gen_DashboardMetrics        = PaperComponents['schemas']['DashboardMetrics'];
// export type Gen_FeedPaper               = PaperComponents['schemas']['FeedPaper'];
// export type Gen_FeedResponse            = PaperComponents['schemas']['FeedResponse'];
// export type Gen_UserStateResponse       = PaperComponents['schemas']['UserStateResponse'];
// export type Gen_CitationRelation        = PaperComponents['schemas']['CitationRelation'];
// export type Gen_GraphNode               = PaperComponents['schemas']['GraphNode'];
// export type Gen_GraphEdge               = PaperComponents['schemas']['GraphEdge'];
// export type Gen_CitationGraphResponse   = PaperComponents['schemas']['CitationGraphResponse'];
// export type Gen_EntityResponse          = PaperComponents['schemas']['EntityResponse'];
// export type Gen_RelationshipResponse    = PaperComponents['schemas']['RelationshipResponse'];
// export type Gen_KnowledgeGraphResponse  = PaperComponents['schemas']['KnowledgeGraphResponse'];
// export type Gen_ExtractionTemplate      = PaperComponents['schemas']['ExtractionTemplate'];
// export type Gen_ExtractionTableRow      = PaperComponents['schemas']['ExtractionTableRow'];
// export type Gen_BatchExtractionResponse = PaperComponents['schemas']['BatchExtractionResponse'];
// export type Gen_SourceConfig            = PaperComponents['schemas']['SourceConfig'];
// export type Gen_TopicResponse           = PaperComponents['schemas']['TopicResponse'];
// export type Gen_TrackedAuthor           = PaperComponents['schemas']['TrackedAuthor'];
// export type Gen_NudgeResponse           = PaperComponents['schemas']['NudgeResponse'];
// export type Gen_DiscoveryResult         = PaperComponents['schemas']['DiscoveryResult'];
// export type Gen_PaperBriefResponse      = PaperComponents['schemas']['PaperBriefResponse'];

// ---------------------------------------------------------------------------
// learning_engine service types (from learning-engine.gen.ts)
// ---------------------------------------------------------------------------
// import type { components as LearningComponents } from './learning-engine.gen';
//
// export type Gen_Deck                    = LearningComponents['schemas']['DeckResponse'];
// export type Gen_Card                    = LearningComponents['schemas']['CardResponse'];
// export type Gen_ReviewResponse          = LearningComponents['schemas']['ReviewResponse'];
// export type Gen_RetentionStats          = LearningComponents['schemas']['RetentionStats'];
// export type Gen_GenerateCardsResponse   = LearningComponents['schemas']['GenerateCardsResponse'];
// export type Gen_ActivityRow             = LearningComponents['schemas']['ActivityRow'];
// export type Gen_RetentionRow            = LearningComponents['schemas']['RetentionRow'];
// export type Gen_ReviewRow               = LearningComponents['schemas']['ReviewRow'];
// export type Gen_LlmCostRow             = LearningComponents['schemas']['LlmCostRow'];

// ---------------------------------------------------------------------------
// For now, the hand-written types in ./index.ts remain the source of truth.
// This file exports nothing until codegen is run.
// ---------------------------------------------------------------------------
export {};

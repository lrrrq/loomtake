const state = {
  uiBuildId: document.querySelector('meta[name="mplus-ui-build"]')?.content || "",
  uiBuildCheckBusy: false,
  uiUpdateAvailable: "",
  uiInputBaselines: new Map(),
  formalReviewSelectedGroup: "focused",
  formalCreative: null,
  formalCreativeViews: new Map(),
  formalFirstDraftViews: new Map(),
  formalFirstDraftEpoch: 0,
  token: "",
  agentAuth: localStorage.getItem("mplus.agent.auth") || "",
  agentWorkspaceId: localStorage.getItem("mplus.agent.workspace") || "",
  agentProjects: [],
  agentSessionId: "",
  agentProjectId: "",
  agentTaskEvents: new Map(),
  agentLocalMessages: [],
  agentPendingActions: new Map(),
  agentCandidateActions: new Map(),
  agentTemporaryActions: new Map(),
  agentSyncTimer: null,
  agentEpoch: 0,
  agentInputDrafts: new Map(),
  agentPreviewUrls: new Map(),
  agentBusyActions: new Set(),
  agentClarificationActions: new Map(),
  agentPreviewSources: new Map(),
  agentCardInputs: new Map(),
  agentOverviewHtml: "",
  lastProjectId: sessionStorage.getItem("mplus.last-project") || "",
  versionTree: null,
  pendingVersionAction: null,
  assetContext: null,
  frozenAssets: [],
  pendingAssetAction: null,
  strategyContext: null,
  knowledgeContext: null,
  pendingGovernanceAction: null,
  formalBriefAvailable: false,
  formalSetupError: "",
  formalOwnerRecoveryRequired: false,
  formalProjects: [],
  formalCategories: [],
  formalCategoryEpoch: 0,
  formalTemplateRoute: "",
  formalTemplateEpoch: 0,
  formalTemplates: null,
  formalTemplateRef: "",
  formalTemplateInputRevision: 0,
  formalTemplateBusy: "",
  formalTemplateTrial: null,
  pendingProjectLifecycle: null,
  formalWorkspaceVersion: null,
  formalBrief: null,
  formalBriefUploadLimits: null,
  formalPendingBriefKey: "",
  formalBriefProjectId: "",
  formalBriefEpoch: 0,
  formalBriefInputs: new Map(),
  formalPlan: null,
  formalPlanProjectId: "",
  formalPlanRequest: 0,
  formalPlanState: "idle",
  formalPlanBusy: false,
  pendingPlanConflictAction: null,
  formalScriptAvailable: false,
  formalScript: null,
  formalScriptBusy: false,
  formalScriptProjectId: "",
  formalScriptDirtyCells: new Map(),
  formalScriptComposingControls: new Set(),
  formalScriptSaveTimer: null,
  formalScriptSavePromise: null,
  formalScriptSaveInFlight: false,
  formalScriptSaveError: false,
  formalScriptRejectedCells: new Map(),
  formalScriptSaveConflict: null,
  formalScriptSaveGeneration: 0,
  formalScriptEpoch: 0,
  formalScriptSaveRetryPending: null,
  formalScriptSaveInFlightPending: null,
  formalScriptDiscarding: false,
  formalScriptDiff: null,
  formalScriptDiffBusy: false,
  formalScriptRevisionProposal: null,
  formalScriptProposalRequest: 0,
  formalScriptProposalInstruction: "",
  formalScriptLockEditing: false,
  formalScriptFeedbackKind: "",
  formalScriptScopeTask: null,
  formalImages: null,
  formalImageProjectVersion: null,
  formalImagePreviewUrls: new Map(),
  formalImageThumbnailUrls: new Map(),
  formalImageEpoch: 0,
  formalImageBusy: false,
  formalImageAutoBusy: false,
  formalImageWorkspace: null,
  formalImageProjectId: "",
  formalImageDirty: false,
  formalImageInputRevision: 0,
  formalImageSearchRequests: new Map(),
  formalDefaultImageBusy: false,
  formalDefaultImageConfirming: false,
  formalImageVersionBusy: false,
  formalImageVersionProposal: null,
  formalDefaultImages: null,
  formalDefaultImageProposal: null,
  formalDefaultImageContext: "",
  formalDefaultImageRequest: 0,
  formalDefaultImageLoadedRequest: 0,
  formalDefaultImageInputRevision: 0,
  formalDefaultImageSelections: new Map(),
  formalDefaultLibrarySelected: new Map(),
  formalDefaultImageView: "library",
  formalDefaultImageMatchScope: "all",
  formalDefaultImageFilters: {},
  formalDefaultImageQuery: "",
  formalDefaultImagePage: 1,
  formalDefaultImageShotNo: "",
  historicalContinuationProposal: null,
  historicalReviewProposal: null,
  formalPlanTextOnly: false,
  formalPlanZeroImage: false,
  formalImageRevisionTaskLoaded: "",
  formalReview: null,
  formalReviewProjectId: "",
  formalReviewBusy: false,
  formalReviewEpoch: 0,
  formalDeliveryBusy: false,
  unifiedSystem: null,
  formalAdminAccess: false,
  formalAdminOverview: null,
  formalAdminTask: null,
  formalAdminProposal: null,
  formalTaskEpoch: 0,
  formalTaskPage: null,
  homeTaskEpoch: 0,
  formalAdminEpoch: 0,
  formalAdminDetailEpoch: 0,
  formalAdminSelected: "",
  formalAdminScroll: 0,
  workbenchProjectId: "",
  workbenchPage: "",
  workbenchEpoch: 0,
  workbenchSessionId: "",
  assetRequest: 0,
  governanceRequest: 0,
  versionRequest: 0,
  versionPreviewRequest: 0,
};

const elements = {
  creativeWorkspace: document.querySelector("#page-script"),
  creativeShotList: document.querySelector("#creative-shot-list"),
  creativeShotSearch: document.querySelector("#creative-shot-search"),
  creativeShotCount: document.querySelector("#creative-shot-count"),
  creativeSelectionCount: document.querySelector("#creative-shot-selection-count"),
  creativeNextLabel: document.querySelector("#creative-next-label"),
  creativeNextCopy: document.querySelector("#creative-next-copy"),
  creativeNextAction: document.querySelector("#creative-next-action"),
  creativeCurrentImage: document.querySelector("#creative-current-image"),
  creativeAssistantScope: document.querySelector("#creative-assistant-scope"),
  creativeAssistantScopeCopy: document.querySelector("#creative-assistant-scope-copy"),
  creativeImageScope: document.querySelector("#creative-image-scope"),
  creativeImageScopeCopy: document.querySelector("#creative-image-scope-copy"),
  creativeImageDraftGate: document.querySelector("#creative-image-draft-gate"),
  creativeLocationNotice: document.querySelector("#creative-location-notice"),
  creativeMaterialsContext: document.querySelector("#creative-materials-context"),
  creativeReviewDraft: document.querySelector("#creative-review-draft"),
  creativeReviewReturnNote: document.querySelector("#creative-review-return-note"),
  creativeReviewBatchReturn: document.querySelector("#creative-review-batch-return"),
  creativeAnalysisBatch: document.querySelector("#creative-analysis-batch"),
  creativeAnalysisCount: document.querySelector("#creative-analysis-count"),
  creativeAnalysisConfirm: document.querySelector("#creative-analysis-confirm"),
  creativeAnalysisResult: document.querySelector("#creative-analysis-result"),
  uiUpdateBanner: document.querySelector("#ui-update-banner"),
  uiUpdateCopy: document.querySelector("#ui-update-copy"),
  formalPlanTemplateSummary: document.querySelector("#formal-plan-template-summary"),
  toast: document.querySelector("#toast"),
  systemGrid: document.querySelector("#system-grid"),
  agentProjectSelect: document.querySelector("#agent-project-select"),
  agentRefresh: document.querySelector("#agent-refresh"),
  agentSessionStatus: document.querySelector("#agent-session-status"),
  agentProjectName: document.querySelector("#agent-project-name"),
  agentProjectMeta: document.querySelector("#agent-project-meta"),
  agentVersionState: document.querySelector("#agent-version-state"),
  agentVersionMeta: document.querySelector("#agent-version-meta"),
  agentReviewState: document.querySelector("#agent-review-state"),
  agentReviewMeta: document.querySelector("#agent-review-meta"),
  agentBlockerTitle: document.querySelector("#agent-blocker-title"),
  agentBlockerDetail: document.querySelector("#agent-blocker-detail"),
  agentNextStep: document.querySelector("#agent-next-step"),
  agentFeed: document.querySelector("#agent-feed"),
  agentMessageForm: document.querySelector("#agent-message-form"),
  agentMessage: document.querySelector("#agent-message"),
  agentMessageHint: document.querySelector("#agent-message-hint"),
  agentMessageSubmit: document.querySelector("#agent-message-submit"),
  versionIdentityNote: document.querySelector("#version-identity-note"),
  versionDesk: document.querySelector("#version-desk"),
  versionProjectSelect: document.querySelector("#version-project-select"),
  versionRefresh: document.querySelector("#version-refresh"),
  versionCount: document.querySelector("#version-count"),
  versionTree: document.querySelector("#version-tree"),
  versionLeft: document.querySelector("#version-left"),
  versionRight: document.querySelector("#version-right"),
  versionCompare: document.querySelector("#version-compare"),
  versionDiff: document.querySelector("#version-diff"),
  versionPreview: document.querySelector("#version-preview"),
  versionGovernanceSelect: document.querySelector("#version-governance-select"),
  versionLifecycle: document.querySelector("#version-lifecycle"),
  versionProjectionNote: document.querySelector("#version-projection-note"),
  versionConfirmation: document.querySelector("#version-confirmation"),
  versionConfirmRisk: document.querySelector("#version-confirm-risk"),
  versionConfirmTitle: document.querySelector("#version-confirm-title"),
  versionConfirmCopy: document.querySelector("#version-confirm-copy"),
  versionConfirmExpiry: document.querySelector("#version-confirm-expiry"),
  versionConfirmCheck: document.querySelector("#version-confirm-check"),
  versionConfirmExecute: document.querySelector("#version-confirm-execute"),
  versionConfirmCancel: document.querySelector("#version-confirm-cancel"),
  assetIdentityNote: document.querySelector("#asset-identity-note"),
  assetDesk: document.querySelector("#asset-desk"),
  assetProjectSelect: document.querySelector("#asset-project-select"),
  assetPackageLabel: document.querySelector("#asset-package-label"),
  assetRefresh: document.querySelector("#asset-refresh"),
  assetLocalPath: document.querySelector("#asset-local-path"),
  assetUploadFile: document.querySelector("#asset-upload-file"),
  assetChoose: document.querySelector("#asset-choose"),
  assetFreeze: document.querySelector("#asset-freeze"),
  assetFrozenList: document.querySelector("#asset-frozen-list"),
  assetScope: document.querySelector("#asset-scope"),
  assetStage: document.querySelector("#asset-stage"),
  assetSearchQuery: document.querySelector("#asset-search-query"),
  assetSearchKind: document.querySelector("#asset-search-kind"),
  assetSearch: document.querySelector("#asset-search"),
  assetSearchResults: document.querySelector("#asset-search-results"),
  assetImports: document.querySelector("#asset-imports"),
  assetConfirmation: document.querySelector("#asset-confirmation"),
  assetConfirmRisk: document.querySelector("#asset-confirm-risk"),
  assetConfirmTitle: document.querySelector("#asset-confirm-title"),
  assetConfirmCopy: document.querySelector("#asset-confirm-copy"),
  assetConfirmExpiry: document.querySelector("#asset-confirm-expiry"),
  assetConfirmCheck: document.querySelector("#asset-confirm-check"),
  assetConfirmExecute: document.querySelector("#asset-confirm-execute"),
  assetConfirmCancel: document.querySelector("#asset-confirm-cancel"),
  governanceIdentityNote: document.querySelector("#governance-identity-note"),
  governanceDesk: document.querySelector("#governance-desk"),
  governanceProjectSelect: document.querySelector("#governance-project-select"),
  governanceRefresh: document.querySelector("#governance-refresh"),
  strategyReadiness: document.querySelector("#strategy-readiness"),
  strategyGrid: document.querySelector("#strategy-grid"),
  strategyClarification: document.querySelector("#strategy-clarification"),
  knowledgeVersion: document.querySelector("#knowledge-version"),
  knowledgeRole: document.querySelector("#knowledge-role"),
  knowledgeRules: document.querySelector("#knowledge-rules"),
  knowledgeScope: document.querySelector("#knowledge-scope"),
  knowledgeStatement: document.querySelector("#knowledge-statement"),
  knowledgeEvidence: document.querySelector("#knowledge-evidence"),
  knowledgePropose: document.querySelector("#knowledge-propose"),
  pendingKnowledgeCount: document.querySelector("#pending-knowledge-count"),
  pendingKnowledge: document.querySelector("#pending-knowledge"),
  governanceConfirmation: document.querySelector("#governance-confirmation"),
  governanceConfirmRisk: document.querySelector("#governance-confirm-risk"),
  governanceConfirmTitle: document.querySelector("#governance-confirm-title"),
  governanceWillChange: document.querySelector("#governance-will-change"),
  governanceWillNotChange: document.querySelector("#governance-will-not-change"),
  governanceModelCalls: document.querySelector("#governance-model-calls"),
  governanceImpactScope: document.querySelector("#governance-impact-scope"),
  governanceConfirmExpiry: document.querySelector("#governance-confirm-expiry"),
  governanceConfirmCheck: document.querySelector("#governance-confirm-check"),
  governanceConfirmExecute: document.querySelector("#governance-confirm-execute"),
  governanceConfirmCancel: document.querySelector("#governance-confirm-cancel"),
  projectsTitle: document.querySelector("#projects-title"),
  projectsCopy: document.querySelector("#projects-copy"),
  formalProjectCreate: document.querySelector("#formal-project-create"),
  formalProjectNew: document.querySelector("#formal-project-new"),
  formalProjectToolbar: document.querySelector("#formal-project-toolbar"),
  formalProjectDashboard: document.querySelector("#formal-project-dashboard"),
  formalOwnerOnboarding: document.querySelector("#formal-owner-onboarding"),
  formalOwnerConfirm: document.querySelector("#formal-owner-confirm"),
  formalOwnerConfirmCopy: document.querySelector("#formal-owner-confirm-copy"),
  formalOwnerEnable: document.querySelector("#formal-owner-enable"),
  formalProjectCreateCancel: document.querySelector("#formal-project-create-cancel"),
  formalProjectName: document.querySelector("#formal-project-name"),
  formalProjectCategory: document.querySelector("#formal-project-category"),
  formalCategoryStatus: document.querySelector("#formal-category-status"),
  formalCategoryRetry: document.querySelector("#formal-category-retry"),
  formalProjectCreateSubmit: document.querySelector("#formal-project-create-submit"),
  formalTemplateCurrent: document.querySelector("#formal-template-current"),
  formalTemplatePicker: document.querySelector("#formal-template-picker"),
  formalTemplateNotice: document.querySelector("#formal-template-notice"),
  formalTemplateRetry: document.querySelector("#formal-template-retry"),
  formalTemplateSelect: document.querySelector("#formal-template-select"),
  formalTemplateFile: document.querySelector("#formal-template-file"),
  formalTemplateImport: document.querySelector("#formal-template-import"),
  formalTemplatePreview: document.querySelector("#formal-template-preview"),
  formalTemplateTrialButton: document.querySelector("#formal-template-trial"),
  formalTemplateTrialHint: document.querySelector("#formal-template-trial-hint"),
  formalTemplateTrialResult: document.querySelector("#formal-template-trial-result"),
  formalTemplateBinding: document.querySelector("#formal-template-binding"),
  formalTemplateBindingCopy: document.querySelector("#formal-template-binding-copy"),
  formalTemplateConfirm: document.querySelector("#formal-template-confirm"),
  formalTemplateApply: document.querySelector("#formal-template-apply"),
  formalPlanTemplate: document.querySelector("#formal-plan-template"),
  formalPlanTemplateLink: document.querySelector("#formal-plan-template-link"),
  assetTemplateLink: document.querySelector("#asset-template-link"),
  formalProjectSearch: document.querySelector("#formal-project-search"),
  formalProjectCount: document.querySelector("#formal-project-count"),
  formalProjectList: document.querySelector("#formal-project-list"),
  formalArchivedCount: document.querySelector("#formal-archived-count"),
  formalArchivedList: document.querySelector("#formal-archived-list"),
  formalOverviewState: document.querySelector("#formal-overview-state"),
  formalArchiveBanner: document.querySelector("#formal-archive-banner"),
  formalArchiveBannerCopy: document.querySelector("#formal-archive-banner-copy"),
  formalArchiveManage: document.querySelector("#formal-archive-manage"),
  formalProjectLifecycle: document.querySelector("#formal-project-lifecycle"),
  formalLifecycleState: document.querySelector("#formal-lifecycle-state"),
  formalLifecycleCopy: document.querySelector("#formal-lifecycle-copy"),
  formalLifecycleForm: document.querySelector("#formal-lifecycle-form"),
  formalLifecycleReason: document.querySelector("#formal-lifecycle-reason"),
  formalLifecyclePrepare: document.querySelector("#formal-lifecycle-prepare"),
  formalLifecycleConfirm: document.querySelector("#formal-lifecycle-confirm"),
  formalLifecycleConfirmTitle: document.querySelector("#formal-lifecycle-confirm-title"),
  formalLifecycleWillChange: document.querySelector("#formal-lifecycle-will-change"),
  formalLifecycleWillNotChange: document.querySelector("#formal-lifecycle-will-not-change"),
  formalLifecycleConfirmCheck: document.querySelector("#formal-lifecycle-confirm-check"),
  formalLifecycleConfirmLabel: document.querySelector("#formal-lifecycle-confirm-label"),
  formalLifecycleExecute: document.querySelector("#formal-lifecycle-execute"),
  formalLifecycleCancel: document.querySelector("#formal-lifecycle-cancel"),
  formalBriefReadiness: document.querySelector("#formal-brief-readiness"),
  formalBriefProjectRequired: document.querySelector("#formal-brief-project-required"),
  formalBriefWorkbench: document.querySelector("#formal-brief-workbench"),
  formalBriefUpload: document.querySelector("#formal-brief-upload"),
  formalBriefDropzone: document.querySelector("#formal-brief-dropzone"),
  formalBriefFiles: document.querySelector("#formal-brief-files"),
  formalBriefSelection: document.querySelector("#formal-brief-selection"),
  formalBriefClearFiles: document.querySelector("#formal-brief-clear-files"),
  formalBriefLimits: document.querySelector("#formal-brief-limits"),
  formalBriefTextCount: document.querySelector("#formal-brief-text-count"),
  formalBriefText: document.querySelector("#formal-brief-text"),
  formalBriefUploadButton: document.querySelector("#formal-brief-upload-button"),
  formalBriefFilesState: document.querySelector("#formal-brief-files-state"),
  formalFactCount: document.querySelector("#formal-fact-count"),
  formalFactList: document.querySelector("#formal-fact-list"),
  formalConflictCount: document.querySelector("#formal-conflict-count"),
  formalConflictList: document.querySelector("#formal-conflict-list"),
  formalBriefRouteState: document.querySelector("#formal-brief-route-state"),
  formalBriefRouteCopy: document.querySelector("#formal-brief-route-copy"),
  formalBriefRouteDetails: document.querySelector("#formal-brief-route-details"),
  formalBriefGateTitle: document.querySelector("#formal-brief-gate-title"),
  formalBriefGateCopy: document.querySelector("#formal-brief-gate-copy"),
  formalBriefConfirm: document.querySelector("#formal-brief-confirm"),
  formalPlanProjectRequired: document.querySelector("#formal-plan-project-required"),
  formalPlanWorkbench: document.querySelector("#formal-plan-workbench"),
  formalPlanState: document.querySelector("#formal-plan-state"),
  formalPlanStatusCopy: document.querySelector("#formal-plan-status-copy"),
  creativeCurrentStructure: document.querySelector("#creative-current-structure"),
  formalPlanBriefBlock: document.querySelector("#formal-plan-brief-block"),
  formalPlanStandardBlock: document.querySelector("#formal-plan-standard-block"),
  formalPlanStandardBlockTitle: document.querySelector("#formal-plan-standard-block-title"),
  formalPlanStandardBlockCopy: document.querySelector("#formal-plan-standard-block-copy"),
  formalPlanImageBlock: document.querySelector("#formal-plan-image-block"),
  formalPlanImageBlockTitle: document.querySelector("#formal-plan-image-block-title"),
  formalPlanImageBlockCopy: document.querySelector("#formal-plan-image-block-copy"),
  formalPlanAction: document.querySelector("#formal-plan-action"),
  formalPlanCreate: document.querySelector("#formal-plan-create"),
  formalPlanPreview: document.querySelector("#formal-plan-preview"),
  formalPlanSummary: document.querySelector("#formal-plan-summary"),
  formalPlanEvents: document.querySelector("#formal-plan-events"),
  formalPlanCapacity: document.querySelector("#formal-plan-capacity"),
  formalPlanCapacityDetails: document.querySelector("#formal-plan-capacity-details"),
  formalPlanShotsDetails: document.querySelector("#formal-plan-shots-details"),
  formalPlanShotsSummary: document.querySelector("#formal-plan-shots-summary"),
  formalPlanDecisionCounts: document.querySelector("#formal-plan-decision-counts"),
  formalPlanConflicts: document.querySelector("#formal-plan-conflicts"),
  formalPlanConflictList: document.querySelector("#formal-plan-conflict-list"),
  formalPlanConflictPrepare: document.querySelector("#formal-plan-conflict-prepare"),
  formalPlanConflictConfirmation: document.querySelector("#formal-plan-conflict-confirmation"),
  formalPlanConflictConfirmCopy: document.querySelector("#formal-plan-conflict-confirm-copy"),
  formalPlanConflictWillChange: document.querySelector("#formal-plan-conflict-will-change"),
  formalPlanConflictWillNotChange: document.querySelector("#formal-plan-conflict-will-not-change"),
  formalPlanConflictCheck: document.querySelector("#formal-plan-conflict-check"),
  formalPlanConflictExecute: document.querySelector("#formal-plan-conflict-execute"),
  formalPlanConflictCancel: document.querySelector("#formal-plan-conflict-cancel"),
  formalPlanCoverage: document.querySelector("#formal-plan-coverage"),
  formalPlanShots: document.querySelector("#formal-plan-shots"),
  formalPlanAudit: document.querySelector("#formal-plan-audit"),
  formalPlanConfirmation: document.querySelector("#formal-plan-confirmation"),
  formalPlanConfirmCheck: document.querySelector("#formal-plan-confirm-check"),
  formalPlanConfirm: document.querySelector("#formal-plan-confirm"),
  formalPlanResult: document.querySelector("#formal-plan-result"),
  formalPlanScriptRows: document.querySelector("#formal-plan-script-rows"),
  formalScriptState: document.querySelector("#formal-script-state"),
  formalScriptProjectRequired: document.querySelector("#formal-script-project-required"),
  formalScriptWorkbench: document.querySelector("#formal-script-workbench"),
  formalScriptProjectName: document.querySelector("#formal-script-project-name"),
  formalScriptVersionLabel: document.querySelector("#formal-script-version-label"),
  formalScriptSaveState: document.querySelector("#formal-script-save-state"),
  formalScriptUndo: document.querySelector("#formal-script-undo"),
  formalScriptRedo: document.querySelector("#formal-script-redo"),
  formalScriptRetry: document.querySelector("#formal-script-retry"),
  formalScriptDiscard: document.querySelector("#formal-script-discard"),
  formalScriptRevision: document.querySelector("#formal-script-revision"),
  formalScriptTable: document.querySelector("#formal-script-table"),
  formalScriptPending: document.querySelector("#formal-script-pending"),
  formalScriptProtection: document.querySelector("#formal-script-protection"),
  formalScriptLockSummary: document.querySelector("#formal-script-lock-summary"),
  formalScriptLockEdit: document.querySelector("#formal-script-lock-edit"),
  formalScriptLockClear: document.querySelector("#formal-script-lock-clear"),
  formalScriptLockSave: document.querySelector("#formal-script-lock-save"),
  formalScriptLockCancel: document.querySelector("#formal-script-lock-cancel"),
  formalScriptInstructionForm: document.querySelector("#formal-script-instruction-form"),
  formalScriptInstruction: document.querySelector("#formal-script-instruction"),
  formalScriptQuickFeedback: document.querySelector("#formal-script-quick-feedback"),
  formalScriptScope: document.querySelector("#formal-script-scope"),
  formalScriptScopeCopy: document.querySelector("#formal-script-scope-copy"),
  formalScriptScopeOptions: document.querySelector("#formal-script-scope-options"),
  formalScriptScopeContinue: document.querySelector("#formal-script-scope-continue"),
  formalScriptInstructionSubmit: document.querySelector("#formal-script-instruction-submit"),
  formalScriptRevisionProposal: document.querySelector("#formal-script-revision-proposal"),
  formalScriptSaveProposal: document.querySelector("#formal-script-save-proposal"),
  formalScriptSaveForm: document.querySelector("#formal-script-save-form"),
  formalScriptSaveReason: document.querySelector("#formal-script-save-reason"),
  formalScriptSave: document.querySelector("#formal-script-save"),
  formalScriptReviewWorkbook: document.querySelector("#formal-script-review-workbook"),
  formalScriptVersions: document.querySelector("#formal-script-versions"),
  formalScriptDiff: document.querySelector("#formal-script-diff"),
  formalScriptVersionGovernance: document.querySelector("#formal-script-version-governance"),
  formalStandardCardState: document.querySelector("#formal-standard-card-state"),
  formalStandardCardCopy: document.querySelector("#formal-standard-card-copy"),
  formalStandardCardDetails: document.querySelector("#formal-standard-card-details"),
  formalImageState: document.querySelector("#formal-image-state"),
  formalImageProjectRequired: document.querySelector("#formal-image-project-required"),
  formalImageWorkbench: document.querySelector("#formal-image-workbench"),
  formalImageTotal: document.querySelector("#formal-image-total"),
  formalImagePassed: document.querySelector("#formal-image-passed"),
  formalImagePending: document.querySelector("#formal-image-pending"),
  formalImageAttention: document.querySelector("#formal-image-attention"),
  formalImageGate: document.querySelector("#formal-image-gate"),
  formalImageGateTitle: document.querySelector("#formal-image-gate-title"),
  formalImageGateCopy: document.querySelector("#formal-image-gate-copy"),
  formalImageSynchronize: document.querySelector("#formal-image-synchronize"),
  formalImageUnderstand: document.querySelector("#formal-image-understand"),
  formalImageToPlan: document.querySelector("#formal-image-to-plan"),
  formalImageGrid: document.querySelector("#formal-image-grid"),
  formalImageUploadForm: document.querySelector("#formal-image-upload-form"),
  formalImageUploadFiles: document.querySelector("#formal-image-upload-files"),
  formalImageUploadSelection: document.querySelector("#formal-image-upload-selection"),
  formalImageUploadButton: document.querySelector("#formal-image-upload-button"),
  formalImageUploadState: document.querySelector("#formal-image-upload-state"),
  formalImageVersionState: document.querySelector("#formal-image-version-state"),
  formalImageVersionMessage: document.querySelector("#formal-image-version-message"),
  formalImageVersionForm: document.querySelector("#formal-image-version-form"),
  formalImageSupplementFile: document.querySelector("#formal-image-supplement-file"),
  formalImageShotGrid: document.querySelector("#formal-image-shot-grid"),
  formalImageSelectionStatus: document.querySelector("#formal-image-selection-status"),
  formalImageVersionSave: document.querySelector("#formal-image-version-save"),
  formalImageVersionProposal: document.querySelector("#formal-image-version-proposal"),
  formalImageVersionHistory: document.querySelector("#formal-image-version-history"),
  formalShotImageState: document.querySelector("#formal-shot-image-state"),
  formalShotImageProjectRequired: document.querySelector("#formal-shot-image-project-required"),
  formalShotImageWorkbench: document.querySelector("#formal-shot-image-workbench"),
  formalReviewState: document.querySelector("#formal-review-state"),
  formalReviewProjectRequired: document.querySelector("#formal-review-project-required"),
  formalReviewWorkbench: document.querySelector("#formal-review-workbench"),
  formalReviewDecisionTitle: document.querySelector("#formal-review-decision-title"),
  formalReviewDecisionCopy: document.querySelector("#formal-review-decision-copy"),
  formalReviewRevisionProposal: document.querySelector("#formal-review-revision-proposal"),
  formalReviewProblemCount: document.querySelector("#formal-review-problem-count"),
  formalReviewSeriousCount: document.querySelector("#formal-review-serious-count"),
  formalReviewAttentionCount: document.querySelector("#formal-review-attention-count"),
  formalReviewApprovedCount: document.querySelector("#formal-review-approved-count"),
  formalReviewStart: document.querySelector("#formal-review-start"),
  formalReviewBatch: document.querySelector("#formal-review-batch"),
  formalReviewBatchCount: document.querySelector("#formal-review-batch-count"),
  formalReviewBatchApprove: document.querySelector("#formal-review-batch-approve"),
  formalReviewIssues: document.querySelector("#formal-review-issues"),
  formalReviewTraceability: document.querySelector("#formal-review-traceability"),
  formalReviewDecisionCounts: document.querySelector("#formal-review-decision-counts"),
  formalReviewEvidence: document.querySelector("#formal-review-evidence"),
  formalReviewFinalForm: document.querySelector("#formal-review-final-form"),
  formalReviewFinalCopy: document.querySelector("#formal-review-final-copy"),
  formalReviewFinalNote: document.querySelector("#formal-review-final-note"),
  formalReviewFinalSubmit: document.querySelector("#formal-review-final-submit"),
  formalReviewNextAction: document.querySelector("#formal-review-next-action"),
  formalDeliveryCard: document.querySelector("#formal-delivery-card"),
  formalDeliveryExcel: document.querySelector("#formal-delivery-excel"),
  formalDeliveryPdf: document.querySelector("#formal-delivery-pdf"),
  formalDeliveryStatus: document.querySelector("#formal-delivery-status"),
  formalTaskCount: document.querySelector("#formal-task-count"),
  formalTaskProjectRequired: document.querySelector("#formal-task-project-required"),
  formalTaskList: document.querySelector("#formal-task-list"),
  formalTaskFilters: document.querySelector("#formal-task-filters"),
  formalTaskProjectFilter: document.querySelector("#formal-task-project-filter"),
  formalTaskStatusFilter: document.querySelector("#formal-task-status-filter"),
  formalAdminLink: document.querySelector("#formal-admin-link"),
  formalAdminRefresh: document.querySelector("#formal-admin-refresh"),
  formalAdminBlocked: document.querySelector("#formal-admin-blocked"),
  formalAdminConsole: document.querySelector("#formal-admin-console"),
  formalAdminSchema: document.querySelector("#formal-admin-schema"),
  formalAdminOutbox: document.querySelector("#formal-admin-outbox"),
  formalAdminDeadletters: document.querySelector("#formal-admin-deadletters"),
  formalAdminTaskCount: document.querySelector("#formal-admin-task-count"),
  formalAdminTaskList: document.querySelector("#formal-admin-task-list"),
  formalAdminTaskDetail: document.querySelector("#formal-admin-task-detail"),
  formalAdminOperations: document.querySelector("#formal-admin-operations"),
  formalProjectNavigation: document.querySelector("#formal-project-navigation"),
  formalNavProjectName: document.querySelector("#formal-nav-project-name"),
  formalNavProjectState: document.querySelector("#formal-nav-project-state"),
  formalBreadcrumb: document.querySelector("#formal-breadcrumb"),
  formalBreadcrumbProject: document.querySelector("#formal-breadcrumb-project"),
  formalBreadcrumbPage: document.querySelector("#formal-breadcrumb-page"),
  formalMobileContext: document.querySelector("#formal-mobile-context"),
  formalMobileExit: document.querySelector("#formal-mobile-exit"),
  formalMobileMenu: document.querySelector("#formal-mobile-menu"),
  formalPlanToScript: document.querySelector("#formal-plan-to-script"),
};

async function api(path, options = {}, sessionRetried = false) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("X-MPlus-UI-Token", state.token);
  if (path.startsWith("/api/agent") && state.agentAuth) headers.set("X-MPlus-Agent-Auth", state.agentAuth);
  if (path.startsWith("/api/formal") && state.agentAuth) headers.set("X-MPlus-Agent-Auth", state.agentAuth);
  if (path.startsWith("/api/manual") && state.agentAuth) headers.set("X-MPlus-Manual-Auth", state.agentAuth);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const detail = payload?.detail;
    const requestAuth = headers.get('X-MPlus-Agent-Auth') || headers.get('X-MPlus-Manual-Auth');
    if (detail?.code === 'AUTH_SESSION_INVALID' && requestAuth && requestAuth === state.agentAuth) invalidateFormalOwnerSession();
    if (!sessionRetried && response.status === 403 && detail === "本地会话已失效，请刷新页面" && path !== "/api/ui/session") {
      // This guard rejects the request before its handler runs, so one retry is safe.
      const session = await api("/api/ui/session", { cache: "no-store" }, true);
      state.token = String(session?.token || "");
      if (state.token) return api(path, options, true);
    }
    const message = typeof detail === "string" ? detail : detail?.message || "请求没有完成";
    const error = new Error(message);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function invalidateFormalOwnerSession() {
  state.agentAuth = ''; state.agentWorkspaceId = ''; state.agentSessionId = ''; state.agentProjectId = '';
  state.agentEpoch += 1; state.workbenchEpoch += 1;
  if (state.agentSyncTimer) clearTimeout(state.agentSyncTimer);
  for (const timer of state.workbenchImageTimers?.values() || []) clearTimeout(timer);
  state.agentProjects = []; state.agentLocalMessages = [];
  state.agentPendingActions.clear(); state.agentCandidateActions.clear(); state.agentTemporaryActions.clear(); state.agentClarificationActions.clear();
  state.workbenchProposals?.clear();
  localStorage.removeItem('mplus.agent.auth'); localStorage.removeItem('mplus.agent.workspace');
  for (const key of Object.keys(localStorage)) if (key.startsWith('mplus.agent.session.')) localStorage.removeItem(key);
  clearFormalPreviewSession();
  state.formalProjects = []; state.formalProjectsLoaded = false; state.formalProjectsRequest = (state.formalProjectsRequest || 0) + 1;
  elements.agentMessageSubmit.disabled = true;
  elements.agentSessionStatus.textContent = '登录已失效';
  elements.agentFeed.innerHTML = '<p role="status">登录已失效，请返回项目页确认重新进入原工作区。</p>';
  elements.formalOwnerConfirm.checked = false; elements.formalOwnerEnable.disabled = true;
  renderFormalOnboarding({enabled: true});
  renderFormalProjects();
}

async function downloadResponse(response, fallbackName) {
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename\*=utf-8''([^;]+)/i);
  const filename = match ? decodeURIComponent(match[1]) : fallbackName;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function renderSystemChecks(checks) {
  elements.systemGrid.innerHTML = checks.map((check, index) => `
    <article class="system-card ${escapeHtml(check.status)}" data-index="0${index + 1}">
      <span>${check.status === "pass" ? "可用" : check.status === "unverified" ? "未验证" : "需处理"}</span>
      <strong>${escapeHtml(check.name)}</strong>
      <small>${escapeHtml(check.message)}</small>
    </article>`).join("");
}

function renderFormalOnboarding(setup) {
  const hasSession = Boolean(state.agentAuth && state.agentWorkspaceId);
  state.formalOwnerRecoveryRequired = !hasSession && setup?.enabled === true;
  elements.formalOwnerOnboarding.hidden = hasSession;
  if (state.formalOwnerRecoveryRequired) {
    elements.formalOwnerOnboarding.querySelector("p").textContent = "项目数据仍在，但此浏览器的本机会话已失效。明确确认后可重新进入原工作区。";
    elements.formalOwnerConfirmCopy.textContent = "我确认在这台电脑重新进入原工作区";
    elements.formalOwnerEnable.textContent = "重新进入工作区";
    elements.formalOwnerEnable.hidden = false;
    elements.formalOwnerConfirm.closest("label").hidden = false;
  } else if (!hasSession) {
    elements.formalOwnerOnboarding.querySelector("p").textContent = "启用后可创建项目、导入需求资料，并在刷新后继续当前工作。";
    elements.formalOwnerConfirmCopy.textContent = "我确认在这台电脑启用工作区";
    elements.formalOwnerEnable.textContent = "启用本机工作区";
    elements.formalOwnerEnable.hidden = false;
    elements.formalOwnerConfirm.closest("label").hidden = false;
  }
}

async function reissueFormalOwnerSession() {
  const bootstrapResponse = await fetch("/api/bootstrap", { cache: "no-store" });
  const bootstrap = await bootstrapResponse.json().catch(() => ({}));
  if (!bootstrapResponse.ok || !bootstrap.token) throw new Error("本机会话恢复入口不可用，请重启 Loomtake 后重试。");
  const response = await fetch("/api/agent/owner/reissue", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-MPlus-UI-Token": bootstrap.token },
    body: JSON.stringify({ confirmed: true }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.detail?.message || payload?.detail || "本机会话恢复失败");
  return payload;
}

async function enableFormalOwner() {
  if (!elements.formalOwnerConfirm.checked) return;
  elements.formalOwnerEnable.disabled = true;
  try {
    const recovering = state.formalOwnerRecoveryRequired;
    const result = recovering
      ? await reissueFormalOwnerSession()
      : await api("/api/formal/owner", {
        method: "POST",
        body: JSON.stringify({ workspace_name: "Loomtake 本地工作区", display_name: "本机管理员", confirmed: true }),
      });
    clearFormalPreviewSession();
    state.agentAuth = result.auth_token;
    state.agentWorkspaceId = result.workspace_id;
    localStorage.setItem("mplus.agent.auth", result.auth_token);
    localStorage.setItem("mplus.agent.workspace", result.workspace_id);
    state.formalOwnerRecoveryRequired = false;
    state.uiInputBaselines.delete(elements.formalOwnerConfirm);
    await bootstrapFormal();
    await renderFormalRoute();
    showToast(recovering ? "已恢复原工作区和项目记录。" : "工作区已启用。");
  } catch (error) {
    showToast(formalError(error));
  } finally {
    elements.formalOwnerEnable.disabled = !elements.formalOwnerConfirm.checked;
  }
}

async function ensureAgentProjectSession(projectId) {
  if (!projectId) return;
  const storageKey = `mplus.agent.session.${projectId}`;
  let sessionId = localStorage.getItem(storageKey) || "";
  let session = null;
  if (sessionId) {
    try { session = await api(`/api/agent/sessions/${encodeURIComponent(sessionId)}`); }
    catch (error) {
      if (error.payload?.detail?.code === 'AUTH_SESSION_INVALID') throw error;
      if (![403, 404].includes(error.status)) throw error;
      localStorage.removeItem(storageKey);
      sessionId = "";
    }
  }
  if (!sessionId) {
    session = await api("/api/agent/sessions", { method: "POST", body: JSON.stringify({ project_id: projectId }) });
    sessionId = session.session.agent_session_id;
    localStorage.setItem(storageKey, sessionId);
  }
  if ((["assistant", "script"].includes(formalRoute().page) && state.agentProjectId === projectId) || (state.workbenchProjectId === projectId && ["assets", "knowledge"].includes(formalRoute().page))) state.agentSessionId = sessionId;
  return session;
}

function agentMessageStorageKey(projectId) {
  return `mplus.agent.messages.${projectId}`;
}

function loadAgentLocalMessages(projectId) {
  try {
    const parsed = JSON.parse(localStorage.getItem(agentMessageStorageKey(projectId)) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item) => item && item.content) : [];
  } catch (_error) {
    return [];
  }
}

function saveAgentLocalMessages(projectId) {
  localStorage.setItem(agentMessageStorageKey(projectId), JSON.stringify(state.agentLocalMessages.slice(-40)));
}

function agentStatusLabel(status) {
  return {
    received: "已接收", understanding: "正在理解", proposed: "待确认", waiting_confirmation: "待确认",
    executing: "执行中", needs_clarification: "待补充", completed: "已完成", waiting_human_review: "等待人工审核",
    partially_completed: "部分完成", blocked: "暂时无法继续", failed: "失败", cancelled: "已取消",
  }[status] || "处理中";
}

function agentSafeHref(value) {
  const href = String(value || "").trim();
  if (!href.startsWith("/")) return "";
  try {
    const url = new URL(href, window.location.origin);
    if (url.origin !== window.location.origin) return "";
    const projectId = encodeURIComponent(state.agentProjectId || "");
    if (url.pathname === "/tasks" || url.pathname === "/admin" || url.pathname.startsWith(`/projects/${projectId}/`)) return `${url.pathname}${url.search}`;
  } catch (_error) {
    return "";
  }
  return "";
}

function agentSourceLabel(source) {
  const value = typeof source === "string" ? source : source?.label || source?.file || source?.filename || "来源已登记";
  return String(value).replace(/^.*[\\/]/, "");
}

function agentErrorCopy(error) {
  const copy = formalError(error);
  if (typeof copy === "string" && copy) return copy;
  return "服务端没有完成这项操作，请刷新后重新核对。";
}

function agentNextAction(response) {
  const next = response?.next_action || {};
  const href = agentSafeHref(response?.next_href || next.href);
  if (!href) return "";
  const suppliedLabel = response?.next_label || next.label;
  const label = String((!suppliedLabel || suppliedLabel === "继续处理") && /\/script(?:\?|$)/.test(href) ? "查看脚本" : suppliedLabel || "继续处理").slice(0, 40);
  return `<div class="agent-next"><strong>下一步</strong><p>${escapeHtml(String(next.reason || response?.next_reason || "按当前项目状态继续。"))}</p><a class="primary-button compact" href="${escapeHtml(href)}">${escapeHtml(label)}</a></div>`;
}

function agentConfirmation(response, taskId) {
  const candidate = response?.next_confirmation || response?.save_version_confirmation || (Array.isArray(response?.pending_confirmations) ? response.pending_confirmations[0] : null);
  const digest = String(candidate?.proposal_digest || candidate?.digest || "");
  if (!digest || !/^[0-9a-f]{64}$/.test(digest)) return null;
  return { taskId: String(candidate?.task_id || taskId || ""), digest, second: true };
}

function agentImpact(response) {
  const items = [["修改", response?.will_change || response?.summary], ["范围", response?.impact_scope]].filter((item) => item[1]);
  if (!items.length) return "";
  return `<dl>${items.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd></div>`).join("")}</dl>`;
}

function agentTaskActionKey(taskId, digest) {
  return `${String(taskId || "")}:${String(digest || "")}`;
}

function agentProposalHtml(response, taskId) {
  const action = agentConfirmation(response, taskId) || {
    taskId: String(taskId || ""), digest: String(response?.proposal_digest || ""), second: false,
  };
  if (!action.taskId || !/^[0-9a-f]{64}$/.test(action.digest)) return "";
  const key = agentTaskActionKey(action.taskId, action.digest);
  state.agentPendingActions.set(key, { ...action, projectId: state.agentProjectId, epoch: state.agentEpoch });
  const second = action.second || response?.confirmation_stage === "save_version";
  return `<div class="agent-proposal"><strong>${escapeHtml(response?.title || (second ? "确认保存版本" : "请确认本次影响"))}</strong>${formalScriptProposalChanges(response)}${agentImpact(response)}<div class="agent-proposal-actions"><button class="primary-button compact" type="button" data-agent-confirm="${escapeHtml(key)}">${second ? "确认保存版本" : (response?.confirmation_stage === "apply_draft" || response?.draft_only) ? "应用到草稿" : "确认执行"}</button><button class="secondary-button compact" type="button" data-agent-dismiss="${escapeHtml(key)}">取消</button></div></div>`;
}

const imageBatchProposals = new Map();

function imageBatchProposalHtml(response, taskId) {
  imageBatchProposals.set(taskId, response);
  if (["received", "understanding", "executing"].includes(response.status)) return `<div data-image-batch="${escapeHtml(taskId)}"><p role="status">${escapeHtml(response.summary || "正在准备配图建议…")}</p><p>已处理 ${response.progress?.completed || 0} / ${response.progress?.total || response.shots?.length || 0} 项</p><small>可以继续编辑；结果会保留到当前项目。</small></div>`;
  if (response.basis_changed) {
    const noun = response.target_kind === 'creative_block' ? '段' : '镜';
    return `<div class="image-batch-stale" data-image-batch="${escapeHtml(taskId)}"><div><p role="status">旧配图建议已过期：当前稿已变化。</p><button type="button" class="text-button" data-image-batch-check>核对当前参考图</button></div><details><summary>查看保留的原建议</summary><p>原建议仍保留，需要调整时可选中当前内容重新推荐。</p><ul>${response.shots.map(shot => `<li>第 ${escapeHtml(shot.shot_no)} ${noun} · ${escapeHtml(shot.visual)}${shot.reason ? ` · ${escapeHtml(shot.reason)}` : ""}</li>`).join("")}</ul></details></div>`;
  }
  const closed = ["completed", "cancelled"].includes(response.status);
  const saved = new Set(response.saved_target_ids || response.saved_block_ids || response.saved_shot_ids || []);
  const gaps = new Set(response.gap_target_ids || response.gap_block_ids || response.gap_shot_ids || response.shots.filter(shot => shot.state === "gap").map(shot => imageBatchTargetId(shot)));
  const actionable = response.shots.filter(shot => shot.reason_code !== "locked" && shot.selected_candidate_key);
  const eligible = actionable.filter(shot => shot.candidates?.some(candidate => candidate.candidate_key === shot.selected_candidate_key && imageBatchSuggestionEligible(candidate)));
  const chosen = actionable.filter(shot => shot.included).length;
  const selectedGaps = response.shots.filter(shot => gaps.has(imageBatchTargetId(shot)) && shot.prepare_generation !== false).length;
  const cards = response.shots.map(shot => `<article class="agent-candidate" data-image-batch-shot="${escapeHtml(imageBatchTargetId(shot))}">
    <header><strong>第 ${escapeHtml(shot.shot_no)} 镜 · ${escapeHtml(shot.visual)}</strong><button type="button" class="text-button" data-image-batch-locate>回到本镜</button></header>
    ${shot.reason_code !== "locked" && !closed ? `<label><input type="checkbox" data-image-batch-include${shot.included ? " checked" : ""}${shot.selected_candidate_key ? "" : " disabled"}>应用本镜建议</label><button type="button" class="text-button" data-image-batch-adjust>逐镜调整建议</button>` : ""}
    ${shot.retrieval?.executed ? `<small>项目候选 ${shot.retrieval.project_candidates} · 图库 ${shot.retrieval.library_inventory} 张 · 候选 ${shot.retrieval.library_candidates} · 符合条件 ${shot.retrieval.eligible_candidates} · 已筛除 ${shot.retrieval.rejected_count}</small>` : ""}
    ${saved.has(imageBatchTargetId(shot)) ? '<p>图片已保存到当前稿 · 待看图核对</p>' : ""}${shot.reason ? `<p>${escapeHtml(shot.reason)}</p>` : ""}
    ${shot.before?.preview_url ? '<figure><div class="agent-candidate-preview" data-image-batch-before></div><figcaption>此前选择</figcaption></figure>' : ""}
    ${shot.candidates?.length ? `<label>图片选择<select data-image-batch-choice><option value="">保持当前选择</option>${shot.candidates.map(c => `<option value="${escapeHtml(c.candidate_key)}"${c.candidate_key === shot.selected_candidate_key ? " selected" : ""}>${escapeHtml(c.source_label || "图片")} · ${escapeHtml(c.caption || c.summary?.caption || "候选图片")}${!imageBatchSuggestionEligible(c) ? " · 仅供参考" : ""}</option>`).join("")}</select></label><div class="agent-candidate-preview" data-image-batch-preview></div><div data-image-batch-reason></div>` : ""}
    ${gaps.has(imageBatchTargetId(shot)) ? `<label><input type="checkbox" data-image-batch-gap${shot.prepare_generation === false ? '' : ' checked'}>为本镜准备生成新图</label>` : ""}</article>`).join("");
  const markup = `<div data-image-batch="${escapeHtml(taskId)}"><p>${response.mode === "replace_selected" ? "重新推荐所选" : "补齐缺图"} · ${escapeHtml(response.basis_label || "当前稿")} · 本次 ${response.shots.length} 镜 · 实际检索 ${response.shots.filter(s => s.retrieval?.executed).length} · 保留 ${response.shots.filter(s => s.state === "preserved").length} · 建议 ${response.shots.filter(s => s.selected_candidate_key).length} · 仅参考 ${response.shots.filter(s => s.reason_code === "reference_only").length} · 无候选 ${response.shots.filter(s => s.reason_code === "no_candidates").length} · 读取失败 ${response.shots.filter(s => s.reason_code === "read_failed").length}</p>
    ${!closed ? `<div class="ui00-inline-actions"><button type="button" class="text-button" data-image-batch-select="all">全选建议</button><button type="button" class="text-button" data-image-batch-select="none">清空建议选择</button><button type="button" class="text-button" data-image-batch-select="eligible">仅选可处理项</button></div><p data-image-batch-selection-count role="status">应用已选 ${chosen} 镜 · 可处理 ${eligible.length} 镜；仅调整建议，点击“应用所选”后保存进度。</p>` : ""}
    ${gaps.size && response.status !== 'cancelled' ? `<div class="ui00-inline-actions"><button type="button" class="text-button" data-image-batch-gap-select="all">全选缺口</button><button type="button" class="text-button" data-image-batch-gap-select="none">清空缺口选择</button></div><p data-image-batch-gap-count role="status">生成缺口已选 ${selectedGaps} / ${gaps.size} 镜</p>` : ""}
    <div class="creative-image-flow-grid">${cards}</div><p data-image-batch-notice>${escapeHtml(response.summary || "选择保存后仍需看图核对。")}</p>
    ${!closed ? '<button type="button" class="primary-button" data-image-batch-save>应用所选</button><button type="button" class="secondary-button" data-image-batch-cancel>取消建议</button>' : response.status === 'completed' ? '<button type="button" class="secondary-button" data-image-batch-save disabled>选图进度已保存</button>' : ""}
    ${gaps.size && response.status !== 'cancelled' ? '<button type="button" class="primary-button" data-image-batch-generate>查看缺口与生成用途</button>' : ""}
    <button type="button" class="text-button" data-image-batch-check>在当前稿继续核对</button></div>`;
  return response.target_kind === "creative_block" ? markup.replaceAll("本镜", "本段").replaceAll("逐镜", "逐段").replaceAll(" 镜", " 段") : markup;
}

async function loadImageBatchPreviews(root = document) {
  await Promise.allSettled([...root.querySelectorAll("[data-image-batch-before]")].map(async preview => {
    if (preview.dataset.loaded) return;
    const batch = preview.closest("[data-image-batch]"), model = imageBatchProposals.get(batch.dataset.imageBatch);
    const shot = model?.shots.find(item => imageBatchTargetId(item) === preview.closest("[data-image-batch-shot]").dataset.imageBatchShot);
    const href = shot?.before?.preview_url;
    if (!href?.startsWith(`/api/formal/projects/${encodeURIComponent(model.project_id)}/images/`) || href.includes("..")) return;
    try { const url = await requestFormalPreview(href, {model, owner:`image-batch:${batch.dataset.imageBatch}`, priority:1});
      if (!preview.isConnected) return;
      const image = document.createElement("img"); image.src = url; image.alt = "当前配图"; preview.replaceChildren(image); preview.dataset.loaded = "true";
    } catch (error) { if (preview.isConnected) preview.innerHTML = formalPreviewFailureMarkup(error, 'data-image-batch-retry'); }
  }));
  await Promise.allSettled([...root.querySelectorAll("[data-image-batch-choice]")].map(async select => {
    const batch = select.closest("[data-image-batch]"), model = imageBatchProposals.get(batch.dataset.imageBatch);
    const card = select.closest("[data-image-batch-shot]");
    const shot = model?.shots.find(s => imageBatchTargetId(s) === card.dataset.imageBatchShot);
    const candidate = shot?.candidates.find(c => c.candidate_key === select.value);
    const preview = card.querySelector("[data-image-batch-preview]");
    card.querySelector("[data-image-batch-reason]").innerHTML = candidate ? formalImageMatchExplanation(candidate) : "";
    preview.textContent = candidate ? "正在读取图片…" : "本镜保持现状";
    if (!candidate) return;
    const choice = select.value, prefix = `/api/formal/projects/${encodeURIComponent(model.project_id)}/images/`;
    if (!candidate.preview_url?.startsWith(prefix) || candidate.preview_url.includes("..")) { preview.textContent = "预览不可用"; return; }
    try {
      const url = await requestFormalPreview(candidate.preview_url, { model, owner: `image-batch:${batch.dataset.imageBatch}`, priority: 1 });
      if (!select.isConnected || select.value !== choice) return;
      const img = document.createElement("img"); img.src = url; img.alt = candidate.caption || "配图建议"; preview.replaceChildren(img);
    } catch (error) { if (select.isConnected && select.value === choice) preview.innerHTML = formalPreviewFailureMarkup(error, 'data-image-batch-retry'); }
  }));
  root.querySelectorAll('[data-image-batch]').forEach(batch => {
    const model = imageBatchProposals.get(batch.dataset.imageBatch);
    const button = batch.querySelector('[data-image-batch-save]');
    if (button) button.disabled = ['completed', 'cancelled'].includes(model?.status) || !model?.shots.some(s => s.included && s.selected_candidate_key);
    if (["completed", "cancelled"].includes(model?.status)) batch.querySelectorAll('select').forEach(s => { s.disabled = true; });
  });
}

function imageBatchPath(model, taskId = "", action = "") {
  return `/api/formal/projects/${encodeURIComponent(model.project_id)}/image-workspace/assistant${taskId ? '/' + encodeURIComponent(taskId) : ''}${action}?workspace_id=${encodeURIComponent(model.workspace_id)}`;
}

function renderImageBatchResult(result) {
  const taskId = result.task.task_id;
  result.response.status = result.task.status;
  if (creativeState().imageFlow?.task?.task_id === taskId) creativeState().imageFlow = result;
  const roots = [...document.querySelectorAll('[data-image-batch]')].filter(b => b.dataset.imageBatch === taskId);
  for (const batch of roots) {
    const holder = document.createElement('div'); holder.innerHTML = imageBatchProposalHtml(result.response, taskId);
    batch.replaceWith(holder.firstElementChild);
  }
  imageBatchProposals.set(taskId, result.response);
  scheduleWorkbenchImagePoll(result);
  void loadImageBatchPreviews();
}

async function reviseImageBatch(taskId, updates) {
  const model = imageBatchProposals.get(taskId);
  const batches = [...document.querySelectorAll('[data-image-batch]')].filter(b => b.dataset.imageBatch === taskId);
  batches.forEach(b => b.querySelectorAll('button,select,input').forEach(c => { c.disabled = true; }));
  try {
    if (model.target_kind === "creative_block") updates = updates.map(({shot_id, ...update}) => ({...update, block_id: update.block_id || shot_id}));
    const result = await api(imageBatchPath(model, taskId), {method: 'PATCH', body: JSON.stringify({proposal_digest: model.proposal_digest, updates})});
    renderImageBatchResult(result);
    return true;
  } catch (error) {
    batches.forEach(b => {
      b.querySelectorAll('button,select,input').forEach(c => { c.disabled = false; });
      b.querySelector('[data-image-batch-notice]').textContent = `${formalError(error)} 当前页面选择保留，可重试本次调整。`;
    });
    return false;
  }
}

async function restoreImageBatch(projectId) {
  const currentHost = imageBatchPresentationHost;
  const host = currentHost(), workspaceId = state.agentWorkspaceId, epoch = state.formalImageEpoch;
  const flow = creativeState().imageFlow;
  const latestTaskId = flow?.response?.project_id === projectId ? flow.task?.task_id : "";
  const alreadyVisible = () => host && [...host.querySelectorAll('[data-image-batch]')]
    .some(batch => imageBatchProposals.get(batch.dataset.imageBatch)?.project_id === projectId
      && (!latestTaskId || batch.dataset.imageBatch === latestTaskId));
  const active = () => formalRoute().projectId === projectId && state.agentWorkspaceId === workspaceId
    && state.formalImageEpoch === epoch && currentHost() === host;
  if (!host) return;
  if (alreadyVisible()) {
    if (active()) host.hidden = false;
    renderWorkbenchRecommendationRecovery();
    return;
  }
  if (latestTaskId && active()) {
    host.hidden = false;
    host.innerHTML = imageBatchProposalHtml(imageBatchProposals.get(latestTaskId) || flow.response, latestTaskId);
    void loadImageBatchPreviews(host);
    renderWorkbenchRecommendationRecovery();
    return;
  }
  try {
    const result = await api(imageBatchPath({project_id: projectId, workspace_id: workspaceId}));
    if (!active() || !result.response || alreadyVisible()
        || creativeState().imageFlow?.task?.task_id !== flow?.task?.task_id) return;
    creativeState().imageFlow = result;
    result.response.status = result.task.status;
    scheduleWorkbenchImagePoll(result);
    host.hidden = false; host.innerHTML = imageBatchProposalHtml(result.response, result.task.task_id);
    void loadImageBatchPreviews(host);
    renderWorkbenchRecommendationRecovery();
  } catch (error) { if (active() && !alreadyVisible()) showToast(`配图建议恢复失败：${formalError(error)}`); }
}

async function saveImageBatch(button) {
  const batch = button.closest("[data-image-batch]"), taskId = batch.dataset.imageBatch;
  const model = imageBatchProposals.get(taskId), notice = batch.querySelector("[data-image-batch-notice]");
  const keys = [...batch.querySelectorAll("[data-image-batch-shot]")].filter(card => card.querySelector('[data-image-batch-include]')?.checked).map(card => card.querySelector('[data-image-batch-choice]')?.value).filter(Boolean);
  if (!keys.length) { notice.textContent = "请至少选择一张图片。"; return; }
  button.disabled = true;
  try {
    const result = await api(`/api/formal/projects/${encodeURIComponent(model.project_id)}/image-workspace/assistant/${encodeURIComponent(taskId)}/confirm?workspace_id=${encodeURIComponent(model.workspace_id)}`, { method: "POST", body: JSON.stringify({ proposal_digest: model.proposal_digest, selected_candidate_keys: keys }) });
    const selected = new Set((result.progress?.selections || []).filter(s => s.selection_active !== false && !s.unavailable).map(s => imageBatchTargetId(s)));
    const unchecked = new Set([...batch.querySelectorAll("[data-image-batch-gap]:not(:checked)")].map(input => input.closest("[data-image-batch-shot]").dataset.imageBatchShot));
    model.gap_shot_ids = (model.gap_shot_ids || model.shots.filter(s => s.state === "gap").map(s => imageBatchTargetId(s))).filter(id => !selected.has(id));
    const appliedKeys = new Set(keys);
    model.saved_shot_ids = model.shots.filter(s => appliedKeys.has(s.selected_candidate_key)).map(s => imageBatchTargetId(s));
    for (const shot of model.shots) {
      const card = [...batch.querySelectorAll("[data-image-batch-shot]")].find(node => node.dataset.imageBatchShot === imageBatchTargetId(shot));
      if (card?.querySelector("select")) shot.selected_candidate_key = card.querySelector("select").value;
      if (model.saved_shot_ids.includes(imageBatchTargetId(shot))) { shot.state = "saved"; shot.reason = ""; }
    }
    model.summary = result.summary; model.status = "completed";
    const holder = document.createElement("div"); holder.innerHTML = imageBatchProposalHtml(model, taskId);
    const savedBatch = holder.firstElementChild; batch.replaceWith(savedBatch);
    savedBatch.querySelectorAll("select").forEach(s => { s.disabled = true; });
    savedBatch.querySelectorAll("[data-image-batch-gap]").forEach(input => { input.checked = !unchecked.has(input.closest("[data-image-batch-shot]").dataset.imageBatchShot); });
    const savedButton = savedBatch.querySelector("[data-image-batch-save]");
    if (savedButton) { savedButton.disabled = true; savedButton.textContent = "选图进度已保存"; }
    void loadImageBatchPreviews(savedBatch);
    if (formalRoute().page === "script" && formalRoute().projectId === model.project_id) {
      if (model.target_kind === "creative_block") await refreshFirstCreative(firstCreativeView(model.project_id));
      else { await loadFormalImageWorkspace(model.project_id, model.project_version); await loadFormalShotImagePreviews(model.project_id, model.project_version); }
    }
  } catch (error) { notice.textContent = formalError(error); button.disabled = false; }
}

document.addEventListener("change", event => {
  if (!event.target.matches('[data-image-batch-choice], [data-image-batch-include], [data-image-batch-gap]')) return;
  const batch = event.target.closest('[data-image-batch]'), card = event.target.closest('[data-image-batch-shot]');
  const update = {shot_id: card.dataset.imageBatchShot};
  if (event.target.matches('[data-image-batch-choice]')) update.candidate_key = event.target.value;
  else if (event.target.matches('[data-image-batch-include]')) update.included = event.target.checked;
  else update.prepare_generation = event.target.checked;
  const count = batch.querySelector('[data-image-batch-selection-count]');
  if (count) count.textContent = `应用已选 ${batch.querySelectorAll('[data-image-batch-include]:checked').length} 镜 · 可处理 ${imageBatchProposals.get(batch.dataset.imageBatch).shots.filter(shot => shot.reason_code !== 'locked' && shot.candidates?.some(candidate => candidate.candidate_key === shot.selected_candidate_key && imageBatchSuggestionEligible(candidate))).length} 镜；点击“应用所选”后保存进度。`;
  const gapCount = batch.querySelector('[data-image-batch-gap-count]');
  if (gapCount) gapCount.textContent = `生成缺口已选 ${batch.querySelectorAll('[data-image-batch-gap]:checked').length} / ${batch.querySelectorAll('[data-image-batch-gap]').length} 镜`;
  if (event.target.matches('[data-image-batch-gap]') && imageBatchProposals.get(batch.dataset.imageBatch)?.status === 'completed') return;
  void reviseImageBatch(batch.dataset.imageBatch, [update]);
});
document.addEventListener("click", event => {
  const button = event.target.closest("button"), batch = button?.closest("[data-image-batch]");
  if (!button || !batch) return;
  const model = imageBatchProposals.get(batch.dataset.imageBatch);
  if (button.matches('[data-image-batch-select], [data-image-batch-gap-select]')) {
    const gaps = button.hasAttribute('data-image-batch-gap-select');
    const mode = gaps ? button.dataset.imageBatchGapSelect : button.dataset.imageBatchSelect;
    const updates = [];
    batch.querySelectorAll(gaps ? '[data-image-batch-gap]' : '[data-image-batch-include]').forEach(input => {
      const shotId = input.closest('[data-image-batch-shot]').dataset.imageBatchShot;
      const shot = model.shots.find(item => imageBatchTargetId(item) === shotId);
      if (!gaps && (!shot?.selected_candidate_key || shot.reason_code === 'locked')) return;
      input.checked = mode === 'all' || mode === 'eligible' && shot.candidates?.some(candidate => candidate.candidate_key === shot.selected_candidate_key && imageBatchSuggestionEligible(candidate));
      updates.push({shot_id: shotId, [gaps ? 'prepare_generation' : 'included']: input.checked});
    });
    const count = batch.querySelector(gaps ? '[data-image-batch-gap-count]' : '[data-image-batch-selection-count]');
    if (count) count.textContent = gaps ? `生成缺口已选 ${batch.querySelectorAll('[data-image-batch-gap]:checked').length} / ${batch.querySelectorAll('[data-image-batch-gap]').length} 镜` : `应用已选 ${batch.querySelectorAll('[data-image-batch-include]:checked').length} 镜 · 可处理 ${model.shots.filter(shot => shot.reason_code !== 'locked' && shot.candidates?.some(candidate => candidate.candidate_key === shot.selected_candidate_key && imageBatchSuggestionEligible(candidate))).length} 镜；点击“应用所选”后保存进度。`;
    if (updates.length && !(gaps && model.status === 'completed')) void reviseImageBatch(batch.dataset.imageBatch, updates);
  }
  else if (button.matches("[data-image-batch-save]")) void saveImageBatch(button);
  else if (button.matches('[data-image-batch-retry]')) void loadImageBatchPreviews(button.closest('[data-image-batch-shot]'));
  else if (button.matches('[data-image-batch-adjust]')) {
    const targetId = button.closest('[data-image-batch-shot]').dataset.imageBatchShot;
    if (model.target_kind === "creative_block") void openFirstCreativeImagePicker(targetId);
    else void openCreativeImagePicker('library', targetId, {batchTaskId: batch.dataset.imageBatch});
  }
  else if (button.matches('[data-image-batch-cancel]')) {
    button.disabled = true;
    api(imageBatchPath(model, batch.dataset.imageBatch, '/cancel'), {method: 'POST', body: JSON.stringify({proposal_digest: model.proposal_digest})})
      .then(result => { model.status = 'cancelled'; model.summary = result.response.summary; renderImageBatchResult({task: result.task, response: model}); })
      .catch(error => { button.disabled = false; batch.querySelector('[data-image-batch-notice]').textContent = formalError(error); });
  }
  else if (button.matches("[data-image-batch-locate]")) {
    const targetId = button.closest("[data-image-batch-shot]").dataset.imageBatchShot;
    if (model.target_kind === "creative_block") { firstCreativeView().editMode = "shot"; selectFirstCreativeBlock(firstCreativeView(), targetId); }
    else { creativeState().imageDetail = true; selectCreativeShot(targetId); }
  }
  else if (button.matches("[data-image-batch-check]")) {
    void changeCreativePanel("images").then(() => {
      if (formalRoute().projectId !== model.project_id || formalRoute().panel !== 'images') return;
      creativeState().imageDetail = false;
      renderWorkbenchCards();
      const target = document.querySelector(state.formalScript?.state === 'empty' ? '#creative-first-overview' : '#creative-image-overview');
      target.tabIndex = -1;
      target.focus({preventScroll:true});
      target.scrollIntoView({block:'start'});
    });
  } else if (button.matches("[data-image-batch-generate]")) {
    const ids = [...batch.querySelectorAll("[data-image-batch-gap]:checked")].map(input => input.closest("[data-image-batch-shot]").dataset.imageBatchShot);
    if (!ids.length) { batch.querySelector("[data-image-batch-notice]").textContent = "请先选择需要生成的缺口。"; return; }
    void openCreativeImageBatchCreation(model, ids);
  }
});

async function openCreativeImageBatchCreation(model, ids) {
  if (model.target_kind === "creative_block") {
    if (!await saveFirstCreative(firstCreativeView(model.project_id))) return;
    await window.MPlusImageCreation.openBatch({projectId:model.project_id, workspaceId:model.workspace_id, targetKind:"creative_block", targets:ids.map(targetId => ({targetId})), onAdopt:async () => refreshFirstCreative(firstCreativeView(model.project_id))}); return;
  }
  if (!await flushFormalScriptChanges() || !await flushCreativeProgress({ manual: true })) return;
  const proposalTaskId = [...imageBatchProposals].find(([, value]) => value === model)?.[0];
  await window.MPlusImageCreation.openBatch({ projectId: model.project_id, workspaceId: model.workspace_id,
    deferSelection: model.status !== 'completed', proposalTaskId,
    targets: ids.map(targetId => ({ targetId })), onAdopt: async result => {
      invalidateCreativeMaterials(model.project_id);
      if (result.status === 'pool_saved') {
        if (!proposalTaskId || !await reviseImageBatch(proposalTaskId, [{shot_id: result.shot_id, image_key: result.image_key}]))
          showToast('图片已加入项目素材，请在逐镜调整建议中选择此图。');
        return;
      }
      await loadFormalScript(model.project_id);
      await loadFormalImageWorkspace(model.project_id, state.formalScript.project_version);
    } });
}

function agentImageCandidatesHtml(response, taskId) {
  const candidates = Array.isArray(response?.candidates) ? response.candidates : [];
  const projectId = state.agentProjectId;
  const shotNo = String(response?.shot_no || "");
  const current = response?.current_image;
  const currentHtml = current ? `<div class="agent-current-image"><strong>当前图片</strong>${agentCandidatePreviewHtml(current, `${taskId}:current`, response)}</div>` : "";
  const cards = candidates.map((candidate, index) => {
    const key = `${taskId}:${candidate.candidate_key || index}`;
    state.agentCandidateActions.set(key, { taskId: String(taskId || ""), candidateKey: String(candidate.candidate_key || ""), projectId, epoch: state.agentEpoch });
    const sources = (candidate.sources || []).map(agentSourceLabel).filter(Boolean).slice(0, 3).join(" · ");
    const reuse = Number(candidate.reuse_count || 0) > 0 || candidate.reuse_required;
    const ready = state.agentPreviewUrls.has(agentPreviewCacheKey(candidate, response));
    return `<article class="agent-candidate" data-agent-candidate-card="${escapeHtml(key)}">${agentCandidatePreviewHtml(candidate, key, response)}<header><strong>${escapeHtml(candidate.caption || candidate.summary?.caption || `候选图片 ${index + 1}`)}</strong><span>${reuse ? "需说明复用" : "待核对"}</span></header><details><summary>匹配依据与来源</summary><p>${escapeHtml(candidate.match_basis || candidate.purpose || "请核对图片内容是否符合镜头")}</p>${sources ? `<small>${escapeHtml(sources)}</small>` : ""}${formalImageMatchExplanation(candidate)}</details>${reuse ? `<label><span>复用理由</span><input type="text" maxlength="1000" data-agent-reuse-reason value="${escapeHtml(state.agentCardInputs.get(key) || "")}" placeholder="说明复用原因" /></label>` : ""}<button class="secondary-button compact" type="button" data-agent-image-select="${escapeHtml(key)}"${ready ? "" : " disabled"}>${ready ? "选择这张" : "等待预览"}</button></article>`;
  }).join("");
  const temporaryKey = `${taskId}:temporary-text-basis`;
  state.agentTemporaryActions.set(temporaryKey, { taskId: String(taskId || ""), projectId, epoch: state.agentEpoch });
  const imageHref = formalPath("shot-images", projectId, { focus_shot: shotNo });
  const libraryHref = formalPath("materials", projectId, { return_to: "shot-images", focus_shot: shotNo, pick: "library" });
  return `<div class="agent-candidates">${currentHtml}${candidates.length ? `<div class="agent-candidate-grid">${cards}</div>` : '<p class="agent-inline-warning">当前没有匹配图片，请选创意库图片或补图。</p>'}<div class="ui00-inline-actions"><a class="secondary-button compact" href="${escapeHtml(libraryHref)}">创意库选图</a><a class="secondary-button compact" href="${escapeHtml(imageHref)}">前往本镜补图</a></div><details class="agent-temporary" data-agent-temporary-card="${escapeHtml(temporaryKey)}"><summary>暂时没有合适图片</summary><label><span>暂准理由 · 补图后才能审核交付</span><input type="text" maxlength="1000" data-agent-temporary-reason value="${escapeHtml(state.agentCardInputs.get(temporaryKey) || "")}" placeholder="例如：现场图片待确认" /></label><button class="secondary-button compact" type="button" data-agent-temporary="${escapeHtml(temporaryKey)}">暂准文字依据</button></details></div>`;
}

function agentContextActive(projectId, epoch = state.agentEpoch) {
  const route = formalRoute();
  return state.agentProjectId === projectId && state.agentEpoch === epoch && ["assistant", "script"].includes(route.page) && (!route.projectId || route.projectId === projectId);
}

function agentPreviewCacheKey(candidate, response) {
  return `${state.agentProjectId}:${response?.project_version || ""}:${candidate?.image_key || candidate?.candidate_key || ""}`;
}

function agentCandidatePreviewHtml(candidate, key, response) {
  const cacheKey = agentPreviewCacheKey(candidate, response);
  const url = state.agentPreviewUrls.get(cacheKey);
  let path = String(candidate?.preview_url || "");
  if (!path && candidate?.image_key && response?.project_version != null) {
    path = `/api/formal/projects/${encodeURIComponent(state.agentProjectId)}/images/${encodeURIComponent(candidate.image_key)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(response.project_version)}`;
  }
  const allowedPrefix = `/api/formal/projects/${encodeURIComponent(state.agentProjectId)}/images/`;
  if (path.startsWith(allowedPrefix) && !path.includes("..")) state.agentPreviewSources.set(key, { cacheKey, path, caption: candidate?.caption || candidate?.summary?.caption || "镜头候选图片" });
  return `<div class="agent-candidate-preview" data-agent-preview="${escapeHtml(key)}">${url ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(candidate?.caption || "镜头候选图片")}" />` : `<span>${path ? "正在读取图片…" : "暂无可用预览"}</span>`}</div>`;
}

async function loadAgentCandidatePreviews(projectId, epoch) {
  await Promise.allSettled([...state.agentPreviewSources].map(async ([key, source]) => {
    if (!agentContextActive(projectId, epoch)) return;
    const target = elements.agentFeed.querySelector(`[data-agent-preview="${CSS.escape(key)}"]`);
    if (!target) return;
    try {
      let url = state.agentPreviewUrls.get(source.cacheKey);
      if (!url) {
        url = await requestFormalPreview(source.path, { owner: `agent:${projectId}:${epoch}`, priority: 1 });
        if (!agentContextActive(projectId, epoch) || !target.isConnected) return;
        state.agentPreviewUrls.set(source.cacheKey, url);
      }
      if (!agentContextActive(projectId, epoch) || !target.isConnected) return;
      const image = document.createElement("img");
      image.src = url; image.alt = source.caption; image.loading = "lazy";
      target.replaceChildren(image);
      const choose = elements.agentFeed.querySelector(`[data-agent-image-select="${CSS.escape(key)}"]`);
      if (choose) { choose.disabled = false; choose.textContent = "选择这张"; }
    } catch (_error) {
      if (agentContextActive(projectId, epoch) && target.isConnected) {
        target.innerHTML = `<span>图片未能加载</span><button class="secondary-button compact" type="button" data-agent-preview-retry="${escapeHtml(key)}">重试预览</button>`;
      }
    }
  }));
}

function agentClarificationHtml(response, taskId) {
  const questions = Array.isArray(response.questions) ? response.questions : [];
  const choices = Array.isArray(response.choices) ? response.choices : [];
  const scope = formalScriptScopeCandidates(response);
  const action = { projectId: state.agentProjectId, epoch: state.agentEpoch, taskId, response };
  state.agentClarificationActions.set(taskId, action);
  if (scope.length && response.analysis_task_id) {
    return `<div class="agent-clarification" data-agent-scope="${escapeHtml(taskId)}"><strong>选择修改范围</strong><div>${scope.map((item) => `<label><input type="checkbox" data-agent-scope-shot value="${escapeHtml(item.shot_id)}"${item.selected ? " checked" : ""} /><span>${escapeHtml(item.label)}</span></label>`).join("")}</div><button class="secondary-button compact" type="button" data-agent-scope-continue="${escapeHtml(taskId)}">继续分析所选镜头</button></div>`;
  }
  if (!questions.length && !choices.length) return "";
  return `<div class="agent-clarification">${questions.length ? `<ul>${questions.map((question) => `<li>${escapeHtml(typeof question === "string" ? question : question.question || question.label || "请补充要求")}</li>`).join("")}</ul>` : ""}<div class="ui00-inline-actions">${choices.map((choice, index) => `<button class="secondary-button compact" type="button" data-agent-clarification="${escapeHtml(taskId)}" data-agent-choice-index="${index}">${escapeHtml(typeof choice === "string" ? choice : choice.label || "选择")}</button>`).join("")}${questions.length ? `<button class="secondary-button compact" type="button" data-agent-answer="${escapeHtml(taskId)}">补充说明</button>` : ""}</div></div>`;
}

function chooseAgentClarification(taskId, index) {
  const action = state.agentClarificationActions.get(taskId);
  if (!action || !agentContextActive(action.projectId, action.epoch)) return;
  const choice = action.response.choices?.[index];
  const text = typeof choice === "string" ? choice : choice?.instruction || choice?.prompt || choice?.value || choice?.label || "";
  const existing = elements.agentMessage.value;
  const retained = action.response.type === 'workbench_failed' && existing.trim()
    ? existing.includes(text) ? existing : `${existing}\n\n${text}` : text;
  elements.agentMessage.value = retained;
  elements.agentMessage.focus();
  state.agentInputDrafts.set(action.projectId, retained);
  elements.agentMessageHint.textContent = "补充或核对这条指令，再提交。";
  elements.agentMessageSubmit.disabled = !text.trim();
}

async function continueAgentScope(taskId) {
  const action = state.agentClarificationActions.get(taskId);
  if (!action || !agentContextActive(action.projectId, action.epoch) || state.agentBusyActions.has(taskId)) return;
  const scope = elements.agentFeed.querySelector(`[data-agent-scope="${CSS.escape(taskId)}"]`);
  const selected = [...scope.querySelectorAll("[data-agent-scope-shot]:checked")].map((input) => input.value);
  if (!selected.length) { showToast("请至少选择一个镜头。"); return; }
  const instruction = action.response.instruction || "";
  if (!instruction) { showToast("请前往脚本页补充原始修改要求。"); return; }
  state.agentBusyActions.add(taskId);
  const button = scope.querySelector("button"); button.disabled = true; button.textContent = "分析中…";
  try {
    const model = await api(formalScriptPath(action.projectId));
    if (!agentContextActive(action.projectId, action.epoch)) return;
    await api(`/api/formal/projects/${encodeURIComponent(action.projectId)}/script/draft-proposals?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {
      method: "POST", body: JSON.stringify({
        expected_revision: Number(model.revision || 0), expected_project_version: Number(model.project_version),
        instruction, analysis_task_id: action.response.analysis_task_id, selected_shot_ids: selected,
        idempotency_key: formalRequestKey("script-revision-proposal"), entrypoint: "assistant", feedback_kind: "copy",
      }),
    });
    if (!agentContextActive(action.projectId, action.epoch)) return;
    await loadAgentSession(action.projectId, action.epoch);

  } catch (error) { if (agentContextActive(action.projectId, action.epoch)) showToast(agentErrorCopy(error)); }
  finally { state.agentBusyActions.delete(taskId); if (button.isConnected) { button.disabled = false; button.textContent = "继续分析所选镜头"; } }
}

const agentImageCreationRequests = new Map();
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-agent-image-create]");
  if (!button) return;
  let request = agentImageCreationRequests.get(button.dataset.agentImageCreate);
  if (!request || request.project_id !== formalRoute().projectId || request.workspace_id !== state.agentWorkspaceId) return;
  const auth = state.agentAuth, epoch = state.agentEpoch;
  button.disabled = true;
  try {
    if (request.workbenchTaskId) {
      const latest = await api(`/api/agent/workbench/${encodeURIComponent(request.workbenchTaskId)}`);
      const response = latest.response || {};
      const current = response.task_id === request.workbenchTaskId && response.project_id === request.project_id && response.workspace_id === request.workspace_id
        && !response.undone && response.type !== 'workbench_proposal'
        && response.image_creation_requests?.find(item => item.target_kind === request.target_kind && item.target_id === request.target_id && item.action_index === request.action_index);
      if (!current) { showToast('本次生图要求已变化，请重新读取创作助手中的最新结果。'); return; }
      request = current;
    }
    if (auth !== state.agentAuth || epoch !== state.agentEpoch || request.project_id !== formalRoute().projectId || request.workspace_id !== state.agentWorkspaceId) return;
    await window.MPlusImageCreation.open({projectId:request.project_id, workspaceId:request.workspace_id,
      targetKind:request.target_kind, targetId:request.target_id, additionalInstruction:request.instruction,
      onAdopt:async () => { invalidateCreativeMaterials(request.project_id); await refreshWorkbenchAfterAction(request.project_id); showToast("图片已采用，请核对当前稿配图。"); }});
  } catch (error) { showToast(formalError(error)); }
  finally { if (button.isConnected) button.disabled = false; }
});

function agentResponseHtml(response, taskId, status, taskStatuses) {
  const type = String(response?.type || "agent_response");
  if (type === 'workbench_handoff') return workbenchHandoffResponseHtml(response, taskId);
  if (type.startsWith("workbench_")) return workbenchProposalHtml(response, taskId, status);
  const title = response?.title || ({
    action_proposal: "待确认的提案", formal_image_candidates: "图片候选",
    formal_script_revision_completed: "脚本修改已完成", formal_image_revision_completed: "图片决定已完成",
    formal_generation_completed: "脚本版本已完成", action_blocked: "当前动作被阻断",
    needs_clarification: "需要补充信息", proposal_cancelled: "提案已取消",
    image_creation_requests: "核对生图要求",
  }[type] || "助手回复");
  const summary = response?.summary || response?.message || "已读取当前项目记录。";
  const actionStatus = taskStatuses.get(String(taskId)) ?? status;
  const confirmation = agentConfirmation(response, taskId);
  const confirmationStatus = confirmation && taskStatuses.get(confirmation.taskId);
  let body = `<p>${escapeHtml(String(summary))}</p>`;
  if (type === "action_proposal" && ["proposed", "waiting_confirmation"].includes(actionStatus || "proposed")) body += agentProposalHtml(response, taskId);
  if (type === "formal_image_candidates") body += agentImageCandidatesHtml(response, taskId);
  if (type === "image_batch_proposal") body += imageBatchProposalHtml(response, taskId);
  if (type === "image_creation_request" || type === 'image_creation_requests') body += workbenchActionListHtml(response.actions) + imageCreationRequestButtons(response, taskId);
  if (type !== "action_proposal" && confirmation && (!confirmationStatus || ["proposed", "waiting_confirmation"].includes(confirmationStatus))) body += agentProposalHtml({ ...response, title: "确认保存版本", confirmation_stage: "save_version" }, taskId);
  body += agentClarificationHtml(response, taskId);
  if (response?.version_label) body += `<p class="agent-version-result">已保存：${escapeHtml(String(response.version_label))}</p>`;
  if (response?.error_code) body += `<p class="agent-inline-warning">${escapeHtml(agentErrorCopy({ payload: { detail: { code: response.error_code } } }))}</p>`;
  body += agentNextAction(response);
  return `<article class="agent-message" data-agent-task="${escapeHtml(taskId || "")}"><span>${escapeHtml(agentStatusLabel(status || "completed"))}</span><h3>${escapeHtml(String(title))}</h3>${body}</article>`;
}

function agentProgressHtml(task) {
  const job = task?.job || {};
  const progress = job.progress || {};
  const stages = Array.isArray(job.stages) ? job.stages.filter((item) => item.status && item.status !== "not_started").slice(-4) : [];
  const detail = stages.map((item) => item.status === "completed" ? "已完成" : agentStatusLabel(item.status)).join("、");
  return `<article class="agent-message system"><span>${escapeHtml(agentStatusLabel(task?.status || "executing"))}</span><h3>任务正在处理</h3><p>${escapeHtml(job.message || "服务端正在处理，请稍后刷新。")}</p>${detail ? `<small>最近进度：${escapeHtml(detail)}</small>` : ""}${progress.percent != null ? `<small>当前进度：${escapeHtml(String(progress.percent))}%</small>` : ""}</article>`;
}

function renderAgentConversation(session) {
  for (const card of elements.agentFeed.querySelectorAll("[data-agent-candidate-card], [data-agent-temporary-card]")) {
    const field = card.querySelector("[data-agent-reuse-reason], [data-agent-temporary-reason]");
    if (field) state.agentCardInputs.set(card.dataset.agentCandidateCard || card.dataset.agentTemporaryCard, field.value);
  }
  state.agentPendingActions.clear(); state.agentCandidateActions.clear(); state.agentTemporaryActions.clear();
  state.agentClarificationActions.clear(); state.agentPreviewSources.clear();
  const time = (value) => {
    const text = String(value || "");
    const parsed = Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(text) ? text : `${text}Z`);
    return Number.isFinite(parsed) ? parsed : 0;
  };
  const recordedTimeline = [
    ...state.agentLocalMessages.map((item) => ({ ...item, user: true })),
    ...(session?.timeline || []),
  ].sort((a, b) => time(a.created_at) - time(b.created_at) || Number(a.sequence_no || 0) - Number(b.sequence_no || 0));
  const internalTasks = new Set((session?.tasks || []).filter(task => task.workbench_parent_task_id).map(task => task.task_id));
  const latestTaskEntries = new Map();
  for (const item of recordedTimeline) {
    if (item.workbench_parent_task_id) internalTasks.add(item.task_id);
    if (!item.user && item.task_id) latestTaskEntries.set(item.task_id, item);
  }
  // Task events are append-only; composed child tasks are represented by their parent.
  const timeline = recordedTimeline.filter(item => item.user || !item.task_id
    || !internalTasks.has(item.task_id) && latestTaskEntries.get(item.task_id) === item);
  const taskStatuses = new Map((session?.tasks || []).map(task => [String(task.task_id), task.status]));
  const messages = timeline.map((item) => item.user
    ? `<article class="agent-message agent-user-message"><span>你的指令</span><p>${escapeHtml(item.content)}</p></article>`
    : agentResponseHtml(item.response || {}, item.task_id, item.status, taskStatuses));
  const current = session?.current_task;
  const parentProgress = String(latestTaskEntries.get(current?.task_id)?.response?.type || '').startsWith('workbench_');
  const progress = !internalTasks.has(current?.task_id) && !parentProgress && ["received", "understanding", "executing"].includes(current?.status) ? agentProgressHtml(current) : "";
  const isActive = item => !item.user && ["proposed", "waiting_confirmation", "waiting_user", "needs_clarification", "executing"].includes(taskStatuses.get(String(item.task_id)) || item.status);
  const currentMessages = [], historicalMessages = [];
  messages.forEach((html, index) => (index >= messages.length - 3 || isActive(timeline[index]) ? currentMessages : historicalMessages).push(html));
  const latest = currentMessages.reverse().join("");
  const history = historicalMessages.length ? `<details class="agent-history"><summary>历史对话（${historicalMessages.length}条）</summary>${historicalMessages.reverse().join("")}</details>` : "";
  elements.agentFeed.innerHTML = (progress + latest + history + state.agentOverviewHtml || '<article class="agent-message system"><h3>会话已准备</h3><p>描述你要完成的事。</p></article>');
  elements.agentFeed.scrollTop = 0;
}

async function loadAgentSession(projectId, epoch = state.agentEpoch) {
  const session = await ensureAgentProjectSession(projectId);
  if (!agentContextActive(projectId, epoch)) return;
  const sessionId = session?.session?.agent_session_id || localStorage.getItem(`mplus.agent.session.${projectId}`);
  if (!sessionId) return;
  const current = await api(`/api/agent/sessions/${encodeURIComponent(sessionId)}`);
  if (!agentContextActive(projectId, epoch)) return;
  const running = (current.tasks || []).some((task) => ["received", "understanding", "executing"].includes(task.status));
  if (!running || !state.agentOverviewHtml) await refreshAgentProjectContext(projectId, epoch);
  if (!agentContextActive(projectId, epoch)) return;
  state.agentSessionId = sessionId;
  state.agentLocalMessages = loadAgentLocalMessages(projectId);
  renderAgentConversation(current);
  elements.agentSessionStatus.textContent = "会话已连接";
  elements.agentMessageSubmit.disabled = !elements.agentMessage.value.trim() || state.agentBusyActions.has(`message:${projectId}`);
  void loadAgentCandidatePreviews(projectId, epoch);
  void loadImageBatchPreviews(elements.agentFeed);
  void loadWorkbenchProposalPreviews();
  if (state.agentSyncTimer) clearTimeout(state.agentSyncTimer);
  if (running && agentContextActive(projectId, epoch)) {
    state.agentSyncTimer = setTimeout(() => loadAgentSession(projectId, epoch).catch(() => {
      if (agentContextActive(projectId, epoch)) elements.agentSessionStatus.textContent = "更新失败，可刷新重试";
    }), 1200);
  }
  return current;
}

async function selectAgentProject(projectId) {
  if (!projectId) return;
  if (state.agentProjectId) state.agentInputDrafts.set(state.agentProjectId, elements.agentMessage.value);
  const epoch = ++state.agentEpoch;
  if (state.agentSyncTimer) clearTimeout(state.agentSyncTimer);
  formalPreviewQueue?.release(`agent:${state.agentProjectId}:${epoch - 1}`);
  state.agentPreviewUrls.clear(); state.agentPreviewSources.clear();
  state.agentOverviewHtml = "";
  state.agentProjectId = projectId;
  state.lastProjectId = projectId;
  sessionStorage.setItem("mplus.last-project", projectId);
  elements.agentMessage.value = state.agentInputDrafts.get(projectId) || "";
  elements.agentMessageHint.textContent = "修改会先展示预览。";
  state.agentSessionId = "";
  elements.agentMessageSubmit.disabled = true;
  elements.agentSessionStatus.textContent = "正在读取会话…";
  elements.agentFeed.innerHTML = '<article class="agent-message system"><h3>正在读取项目</h3><p>正在恢复项目会话和任务记录…</p></article>';
  await loadAgentSession(projectId, epoch);
}

async function refreshAgentProjectContext(projectId, epoch) {
  const [projectState, overview] = await Promise.all([
    api(`/api/agent/projects/${encodeURIComponent(projectId)}/state`),
    api(`/api/formal/projects/${encodeURIComponent(projectId)}/overview`),
  ]);
  if (!agentContextActive(projectId, epoch)) return;
  renderAgentState(projectState, overview);
  const stages = (overview.stages || []).map((stage) => `<li><strong>${escapeHtml(stage.label)}</strong><span>${escapeHtml(stage.state)}</span>${stage.detail ? `<small>${escapeHtml(stage.detail)}</small>` : ""}</li>`).join("");
  const next = overview.next_action || {};
  const issues = (overview.blockers || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const overviewHtml = `<article class="agent-message project_state"><h3>项目进度</h3><ol class="ui00-overview-stages">${stages}</ol>${issues ? `<h4>需要处理</h4><ul>${issues}</ul>` : ""}${next.href && next.label ? `<div class="agent-next"><strong>下一步</strong><p>${escapeHtml(next.reason || "按当前项目状态继续。")}</p><a class="primary-button compact" href="${escapeHtml(next.href)}">${escapeHtml(next.label)}</a></div>` : ""}</article>`;
  state.agentOverviewHtml = `<details class="agent-overview"><summary>项目进度与下一步</summary>${overviewHtml}</details>`;
}

async function submitAgentMessage(event) {
  event.preventDefault();
  const content = elements.agentMessage.value.trim();
  const projectId = state.agentProjectId, sessionId = state.agentSessionId, epoch = state.agentEpoch;
  const key = `message:${projectId}`;
  if (!content || !sessionId || !projectId || state.agentBusyActions.has(key)) return;
  let context;
  try { context = await workbenchMessageContext(); }
  catch (error) { elements.agentMessageHint.textContent = error.message; return; }
  const fingerprint = JSON.stringify({projectId, content, context});
  const retry = state.workbenchMessageRetry;
  const operationKey = retry?.fingerprint === fingerprint ? retry.operationKey : crypto.randomUUID();
  state.workbenchMessageRetry = {fingerprint, operationKey};
  const message = { content, created_at: new Date().toISOString() };
  state.agentLocalMessages.push(message); saveAgentLocalMessages(projectId);
  elements.agentMessage.value = ""; state.agentInputDrafts.set(projectId, "");
  state.agentBusyActions.add(key);
  elements.agentMessageSubmit.disabled = true;
  elements.agentMessageHint.textContent = "正在处理你的指令…";
  elements.agentFeed.insertAdjacentHTML("beforeend", `<article class="agent-message agent-user-message"><span>你的指令</span><p>${escapeHtml(content)}</p></article>`);
  try {
    await api(`/api/agent/sessions/${encodeURIComponent(sessionId)}/messages`, { method: "POST", body: JSON.stringify({ content, ...(context ? {context, operation_key: operationKey} : {}) }) });
    state.workbenchMessageRetry = null;
    if (!agentContextActive(projectId, epoch)) return;
    await loadAgentSession(projectId, epoch);
    elements.agentMessageHint.textContent = "已返回结果，请查看下方动作。";
  } catch (error) {
    if (agentContextActive(projectId, epoch)) {
      elements.agentMessageHint.textContent = agentErrorCopy(error);
      if (!elements.agentMessage.value) elements.agentMessage.value = content;
      state.agentInputDrafts.set(projectId, elements.agentMessage.value);
    } else if (!state.agentInputDrafts.get(projectId)) state.agentInputDrafts.set(projectId, content);
  } finally {
    state.agentBusyActions.delete(key);
    if (agentContextActive(projectId, epoch)) elements.agentMessageSubmit.disabled = !elements.agentMessage.value.trim();
  }
}

async function confirmAgentAction(key) {
  const pending = state.agentPendingActions.get(key);
  await runAgentTaskAction(pending, "confirm", { proposal_digest: pending?.digest }, key);
}

async function dismissAgentAction(key) {
  await runAgentTaskAction(state.agentPendingActions.get(key), "dismiss", undefined, key);
}

async function selectAgentImageCandidate(key) {
  const pending = state.agentCandidateActions.get(key);
  const card = elements.agentFeed.querySelector(`[data-agent-candidate-card="${CSS.escape(key)}"]`);
  if (!pending?.candidateKey || card?.querySelector("[data-agent-image-select]")?.disabled) return;
  const reuseReason = card?.querySelector("[data-agent-reuse-reason]")?.value.trim() || "";
  await runAgentTaskAction(pending, "image-selection", { candidate_key: pending.candidateKey, reuse_reason: reuseReason }, key);
}

async function selectAgentTemporaryTextBasis(key) {
  const pending = state.agentTemporaryActions.get(key);
  const card = elements.agentFeed.querySelector(`[data-agent-temporary-card="${CSS.escape(key)}"]`);
  const reason = card?.querySelector("[data-agent-temporary-reason]")?.value.trim() || "";
  if (!pending || !reason) { showToast("请先填写暂准理由。"); return; }
  await runAgentTaskAction(pending, "temporary-text-basis", { reason }, key);
}

async function runAgentTaskAction(pending, action, body, key) {
  if (!pending || !agentContextActive(pending.projectId, pending.epoch) || state.agentBusyActions.has(key)) return;
  state.agentBusyActions.add(key);
  const buttons = [...elements.agentFeed.querySelectorAll("button")].filter((button) => Object.values(button.dataset).includes(key));
  buttons.forEach((button) => { button.disabled = true; });
  elements.agentMessageHint.textContent = "正在处理…";
  try {
    await api(`/api/agent/tasks/${encodeURIComponent(pending.taskId)}/${action}`, { method: "POST", ...(body ? { body: JSON.stringify(body) } : {}) });
    if (!agentContextActive(pending.projectId, pending.epoch)) return;
    await loadAgentSession(pending.projectId, pending.epoch);
    elements.agentMessageHint.textContent = "已更新，请查看结果和下一步。";
  } catch (error) {
    if (agentContextActive(pending.projectId, pending.epoch)) elements.agentMessageHint.textContent = agentErrorCopy(error);
  } finally {
    state.agentBusyActions.delete(key);
    buttons.filter((button) => button.isConnected).forEach((button) => { button.disabled = false; });
  }
}

function workbenchProjectCategory(project) {
  if (!project.category_id) return "待识别";
  const option = [...(elements.formalProjectCategory?.options || [])].find((item) => item.value === project.category_id);
  return project.category_label || option?.textContent || "类别未登记";
}

function renderAgentState(response, overview = null) {
  const project = response.project || {};
  const versions = response.versions || {};
  const blocker = response.blocker || {};
  const stages = overview?.stages || [];
  const scriptStage = stages.find((stage) => stage.id === "script");
  const reviewStage = stages.find((stage) => stage.id === "review");
  elements.agentProjectName.textContent = project.name || "未命名项目";
  elements.agentProjectMeta.textContent = `${workbenchProjectCategory(project)} · ${project.status === "archived" ? "已归档" : project.status === "active" ? "进行中" : "状态待确认"}`;
  elements.agentVersionState.textContent = scriptStage?.state === "已完成" || versions.latest_version_id ? "已有版本" : "尚无版本";
  elements.agentVersionMeta.textContent = Number(versions.count) > 0 ? `共 ${versions.count} 个版本` : scriptStage?.detail || "尚未创建脚本";
  if (reviewStage) {
    elements.agentReviewState.textContent = reviewStage.state === "未开始" ? "尚未审核" : reviewStage.state === "已完成" ? "审核已完成" : reviewStage.state;
    elements.agentReviewMeta.textContent = reviewStage.detail || "完成配图后开始审核";
  } else if (versions.latest_review) {
    elements.agentReviewState.textContent = humanReviewLabel(versions.latest_review);
    elements.agentReviewMeta.textContent = "查看当前审核记录";
  } else {
    elements.agentReviewState.textContent = blocker.code === "WAITING_HUMAN_REVIEW" ? "等待人工审核" : "尚未审核";
    elements.agentReviewMeta.textContent = "完成配图后开始审核";
  }
  const next = overview?.next_action;
  elements.agentBlockerTitle.textContent = next?.label || blocker.title || "状态待确认";
  elements.agentBlockerDetail.textContent = overview
    ? overview.blockers?.[0] || next?.reason || "查看项目进度和下一步。"
    : blocker.detail || "请刷新项目状态。";
  elements.agentNextStep.textContent = next?.label ? `下一步：${next.label}` : `下一步：${blocker.next_step || "查看项目概览"}`;
}

function workbenchReady(note, desk) {
  const ready = Boolean(state.agentAuth && state.agentWorkspaceId && state.agentProjects.length);
  note.hidden = ready;
  desk.hidden = !ready;
  if (!ready) {
    note.textContent = state.agentAuth
      ? "当前工作区还没有项目。请先新建项目并导入需求资料。"
      : "请先返回项目列表进入本机工作区。";
  }
  return ready;
}

function populateWorkbenchProjects(select, selected) {
  const previous = selected === undefined ? select.value : selected;
  select.innerHTML = '<option value="">请选择项目</option>' + state.agentProjects.map((project) => `<option value="${escapeHtml(project.project_id)}">${escapeHtml(project.name)} · ${escapeHtml(workbenchProjectCategory(project))}</option>`).join("");
  select.value = state.agentProjects.some(project => project.project_id === previous) ? previous : "";
}

function workbenchContext() {
  return { projectId: state.workbenchProjectId, page: formalRoute().page, epoch: state.workbenchEpoch, sessionId: state.workbenchSessionId };
}

function workbenchContextActive(context) {
  return context.projectId && state.workbenchProjectId === context.projectId && state.workbenchEpoch === context.epoch && formalRoute().page === context.page;
}

function chooseWorkbenchProject(projectId) {
  const url = new URL(window.location.href);
  if (projectId) url.searchParams.set("project_id", projectId); else url.searchParams.delete("project_id");
  if (!projectId) { state.lastProjectId = ""; sessionStorage.removeItem("mplus.last-project"); }
  if (formalRoute().page === "versions" && formalRoute().projectId && projectId) {
    return navigateFormal(formalPath("script", projectId, {view: "versions"}));
  }
  return navigateFormal(`${url.pathname}${url.search}`);
}

// Keep the original human-confirmed request, scoped to the authenticated actor.
// Server identity works on both localhost and ordinary LAN HTTP origins.
async function manualRecoveryKey(kind, projectId) {
  const auth = state.agentAuth, workspaceId = state.agentWorkspaceId;
  if (state.manualRecoveryIdentity?.auth !== auth) {
    const identity = await api("/api/manual/session");
    if (state.agentAuth !== auth || state.agentWorkspaceId !== workspaceId) throw new Error("当前身份已变化，请重新读取。");
    state.manualRecoveryIdentity = { auth, actorId: identity.actor_id };
  }
  return `mplus.manual-recovery.${workspaceId}.${state.manualRecoveryIdentity.actorId}.${projectId}.${kind}`;
}

async function retainManualRequest(kind, pending) {
  if (!pending.recoveryKey) pending.recoveryKey = await manualRecoveryKey(kind, pending.contextKey.projectId);
  sessionStorage.setItem(pending.recoveryKey, JSON.stringify(pending));
}

function completeManualRequest(pending) {
  if (pending.recoveryKey) sessionStorage.removeItem(pending.recoveryKey);
}

function manualRequestDefinitelyRejected(error) {
  // These domain errors are returned only after the same-database transaction
  // rolled back. Transport failures and permission loss cannot prove no effect.
  return ["INVALID_INPUT", "PROJECT_VERSION_CONFLICT", "REVIEW_EVIDENCE_CONFLICT", "REVIEW_REQUIRED"].includes(error.payload?.detail?.code);
}

async function restoreManualRequest(kind, contextKey) {
  const key = await manualRecoveryKey(kind, contextKey.projectId);
  const stored = sessionStorage.getItem(key);
  if (!stored || !workbenchContextActive(contextKey)) return false;
  let pending;
  try { pending = JSON.parse(stored); } catch { throw new Error("原操作记录无法读取，请核对操作结果后再继续。"); }
  const assetImportReceipt = kind === "asset" && pending?.toolName === "create_asset_import" && pending?.confirmation?.confirmation_token && pending?.originalAgentSessionId;
  if (!(pending?.prepared?.command_token || pending?.confirmation?.command_token || assetImportReceipt) || pending.contextKey?.projectId !== contextKey.projectId) {
    throw new Error("原操作记录不完整，请核对操作结果后再继续。");
  }
  pending = { ...pending, contextKey, recoveryKey: key };
  if (kind === "version") showVersionConfirmation(pending);
  else if (kind === "asset") { state.pendingAssetAction = pending; renderAssetConfirmation(); }
  else showGovernanceConfirmation(pending);
  const prefix = kind === "version" ? "version" : kind === "asset" ? "asset" : "governance";
  elements[`${prefix}ConfirmRisk`].textContent = "上次操作结果待核对";
  elements[`${prefix}ConfirmExpiry`].textContent = "确认后使用原请求恢复；已完成的操作会返回原结果。关闭此面板不会撤销已执行的操作。";
  elements[`${prefix}ConfirmExecute`].textContent = "恢复原操作结果";
  return true;
}

async function loadVersionWorkbench() {
  if (!workbenchReady(elements.versionIdentityNote, elements.versionDesk)) return;
  const contextKey = workbenchContext(), request = ++state.versionRequest;
  const active = () => workbenchContextActive(contextKey) && request === state.versionRequest;
  const projectId = elements.versionProjectSelect.value;
  if (!projectId) return;
  elements.versionRefresh.disabled = true;
  try {
    const tree = await api(`/api/agent/workbench/versions?project_id=${encodeURIComponent(projectId)}`);
    if (!active()) return;
    state.versionTree = tree;
    renderVersionTree(tree);
    await restoreManualRequest("version", contextKey);
  } catch (error) {
    if (!active()) return;
    elements.versionTree.innerHTML = `<div class="workbench-empty">${escapeHtml(error.message)}</div>`;
    showToast(error.message);
  } finally {
    if (active()) elements.versionRefresh.disabled = false;
  }
}

function flattenVersionTree(roots, depth = 0, result = []) {
  (roots || []).forEach((item) => {
    result.push({ ...item, depth });
    flattenVersionTree(item.children || [], depth + 1, result);
  });
  return result;
}

function publicationProjectionStatus(publication) {
  const status = String(publication?.projection_status || "").trim().toLowerCase();
  return ["pending", "failed", "published"].includes(status) ? status : "unknown";
}

function publicationProjectionError(publication) {
  return String(publication?.error || publication?.projection_error || "").trim();
}

function publicationProjectionLabel(status) {
  return ({
    pending: "发布处理中",
    failed: "发布失败",
    published: "已发布",
    unknown: "发布状态待确认",
  })[status] || "发布状态待确认";
}

function publicationProjectionNotice(status, publication = {}) {
  if (status === "pending") return "发布处理中：当前交付版本尚未更新。";
  if (status === "failed") {
    const error = publicationProjectionError(publication);
    return `发布登记未完成${error ? `：${error}` : ""}。当前未发布，可重新确认以重试这次发布。`;
  }
  if (status === "published") return "已发布：当前交付版本已更新。";
  return "发布结果尚未核实，请刷新后查看是否已发布。";
}

function humanReviewState(review) {
  const decision = String(review?.decision || review?.status || "").trim().toLowerCase();
  if (["pending_review", "waiting_human_review"].includes(decision)) return "waiting";
  return decision;
}

function humanReviewLabel(review) {
  const stateValue = humanReviewState(review);
  return ({ waiting: "等待人工审核", quality_pass: "人工审核已通过", quality_fail: "人工审核未通过" })[stateValue]
    || "待登记审核结论";
}

function renderVersionTree(tree) {
  const versions = flattenVersionTree(tree.roots || []);
  elements.versionCount.textContent = `${versions.length} 个版本`;
  elements.versionTree.innerHTML = versions.length ? versions.map((version, index) => {
    const governance = version.governance || {};
    const review = governance.latest_review;
    const publication = governance.latest_publication;
    const reviewState = humanReviewState(review);
    const publicationState = publicationProjectionStatus(publication);
    const publicationBadge = publication
      ? `<span class="${publicationState === "published" ? "is-published" : publicationState === "failed" ? "is-fail" : publicationState === "pending" ? "is-pending" : "is-unknown"}">${publicationProjectionLabel(publicationState)}</span>`
      : "";
    return `<article class="version-node" style="--version-depth:${version.depth}">
      <span>${String(index + 1).padStart(2, "0")}</span>
      <div><strong>版本 ${escapeHtml(version.version_no || index + 1)}</strong><small>${version.is_current ? "当前版本" : "历史版本 · 只读"} · ${escapeHtml(String(version.created_at || "").slice(0, 16).replace("T", " "))}</small></div>
      <div class="version-node-states">
        <span class="${reviewState === "quality_pass" ? "is-pass" : reviewState === "quality_fail" ? "is-fail" : reviewState === "waiting" ? "is-pending" : ""}">${escapeHtml(humanReviewLabel(review))}</span>
        ${governance.is_current_candidate ? `<span class="is-candidate">当前候选</span>` : ""}
        ${publicationBadge}
        <button class="secondary-button compact" type="button" data-version-read="${escapeHtml(version.version_id)}">查看内容</button>
        <button class="secondary-button compact" type="button" data-version-workbook="${escapeHtml(version.version_no || index + 1)}">下载本版复核稿</button>
      </div>
    </article>`;
  }).join("") : `<div class="workbench-empty">尚无已绑定且可验证的版本。先在脚本助手完成真实生成。</div>`;
  const options = versions.map((version, index) => `
    <option value="${escapeHtml(version.version_id)}">版本 ${escapeHtml(version.version_no || index + 1)}${version.is_current ? " · 当前" : " · 历史"}</option>
  `).join("");
  elements.versionLeft.innerHTML = options;
  elements.versionRight.innerHTML = options;
  if (versions.length > 1) elements.versionRight.selectedIndex = versions.length - 1;
  const previousGovernanceVersion = elements.versionGovernanceSelect.value;
  elements.versionGovernanceSelect.innerHTML = options;
  if (versions.some((item) => item.version_id === previousGovernanceVersion)) {
    elements.versionGovernanceSelect.value = previousGovernanceVersion;
  } else if (versions.length) {
    elements.versionGovernanceSelect.value = versions[versions.length - 1].version_id;
  }
  renderVersionLifecycle();
  elements.versionCompare.disabled = versions.length < 2;
  if (versions.length < 2) {
    elements.versionDiff.innerHTML = `<div class="workbench-empty">至少需要两个已验证版本才能比较。</div>`;
  }
  const current = versions.find((item) => item.is_current) || versions[versions.length - 1];
  if (current) loadVersionPreview(current.version_id);
}

async function loadVersionPreview(versionId) {
  const contextKey = workbenchContext(), request = ++state.versionPreviewRequest;
  const active = () => workbenchContextActive(contextKey) && request === state.versionPreviewRequest;
  const projectId = elements.versionProjectSelect.value;
  if (!projectId || !versionId) return;
  elements.versionPreview.innerHTML = '<div class="workbench-empty">正在读取已保存版本…</div>';
  try {
    const value = await api(`/api/agent/workbench/versions/read?project_id=${encodeURIComponent(projectId)}&version_id=${encodeURIComponent(versionId)}`);
    if (!active()) return;
    const rows = (value.shots || []).map((shot) => {
      const fields = shot.fields || {};
      const duration = fields["时长(秒)"] ?? fields.duration_seconds ?? fields["时长"] ?? "—";
      const visual = fields["画面描述"] ?? fields.visual ?? fields.copy ?? "—";
      const sound = fields["声音"] ?? fields.sound ?? fields.voiceover ?? "—";
      return `<tr><td>${escapeHtml(shot.shot_no)}</td><td>${escapeHtml(fields["结构"] ?? fields.structure ?? "—")}</td><td>${escapeHtml(duration)}</td><td>${escapeHtml(visual)}</td><td>${escapeHtml(sound)}</td></tr>`;
    }).join("");
    elements.versionPreview.innerHTML = `<div class="ui00-inline-actions"><strong>版本 ${escapeHtml(value.version_no)}</strong><span>${value.is_current ? "当前版本的只读快照" : "历史版本，只读查看"}</span></div><table class="ui00-script-table"><thead><tr><th>镜头</th><th>结构</th><th>时长</th><th>画面/文案</th><th>声音</th></tr></thead><tbody>${rows}</tbody></table>`;
  } catch (error) {
    if (!active()) return;
    elements.versionPreview.innerHTML = `<div class="workbench-empty">${escapeHtml(error.message)}</div>`;
  }
}

function renderVersionLifecycle() {
  const versions = flattenVersionTree(state.versionTree?.roots || []);
  const version = versions.find((item) => item.version_id === elements.versionGovernanceSelect.value);
  if (!version) {
    elements.versionLifecycle.innerHTML = `<div class="workbench-empty">当前没有可治理版本。</div>`;
    return;
  }
  const governance = version.governance || {};
  const review = governance.latest_review;
  const publication = governance.latest_publication;
  const reviewState = humanReviewState(review);
  const publicationState = publicationProjectionStatus(publication);
  const reviewPass = review?.decision === "quality_pass";
  const reviewRecorded = Boolean(review) && reviewState !== "waiting";
  const steps = [
    {
      index: "01",
      complete: reviewRecorded,
      title: "登记已验证人工结论",
      detail: reviewState === "waiting"
        ? "等待人工审核；这不是技术失败，也不表示质量已通过。"
        : reviewRecorded
          ? `${humanReviewLabel(review)} · 审核证据已锁定`
          : "需要已完成的人工审核结论；等待审核不等于通过。",
      button: reviewRecorded || reviewState === "waiting" ? "" : `<button class="primary-button compact" type="button" data-version-action="record_version_review">准备登记确认</button>`,
    },
    {
      index: "02",
      complete: Boolean(governance.is_current_candidate),
      title: "设为当前候选",
      detail: governance.is_current_candidate
        ? "已设为当前候选"
        : reviewPass ? "人工审核已通过，可以设为候选。" : "请先完成人工审核。",
      button: !governance.is_current_candidate && reviewPass && !publication
        ? `<button class="primary-button compact" type="button" data-version-action="select_candidate_version">准备候选确认</button>` : "",
    },
    {
      index: "03",
      complete: publicationState === "published",
      title: "正式发布登记",
      detail: publicationState === "pending"
        ? "发布处理中；当前交付指针尚未更新。"
        : publicationState === "failed"
          ? `发布失败${publicationProjectionError(publication) ? `：${publicationProjectionError(publication)}` : ""}；当前未发布。`
          : publicationState === "published"
            ? "已发布；当前交付版本已更新。"
            : publication
              ? "发布状态待确认；当前不显示为已发布。"
              : reviewPass && governance.is_current_candidate ? "可以发布；仍需一次独立确认。" : "请先通过人工审核并设为当前候选。",
      button: (publication && ["pending", "published", "unknown"].includes(publicationState)) || !reviewPass || !governance.is_current_candidate
        ? ""
        : `<button class="primary-button compact" type="button" data-version-action="publish_version">${publicationState === "failed" ? "重新发布确认" : "准备发布确认"}</button>`,
    },
  ];
  elements.versionLifecycle.innerHTML = steps.map((step) => `
    <article class="version-step ${step.complete ? "is-complete" : ""}">
      <span>${step.index} · ${step.complete ? "已完成" : "待处理"}</span>
      <h3>${step.title}</h3>
      <p>${escapeHtml(step.detail)}</p>
      ${step.button}
    </article>
  `).join("");
  elements.versionProjectionNote.classList.toggle("is-pending", publicationState === "pending");
  elements.versionProjectionNote.classList.toggle("is-failed", publicationState === "failed");
  elements.versionProjectionNote.textContent = publication
    ? publicationProjectionNotice(publicationState, publication)
    : review?.decision === "quality_fail"
      ? "人工审核未通过，当前不能设为候选或发布。"
      : "请先完成人工审核，再设为候选并发布。";
  if (Array.isArray(state.versionTree?.allowed_actions)) {
    elements.versionLifecycle.querySelectorAll("[data-version-action]").forEach(button => {
      if (state.versionTree.allowed_actions.includes(button.dataset.versionAction)) return;
      button.disabled = true;
      button.insertAdjacentHTML("afterend", '<small>当前账号没有此项治理权限；仍可查看和比较版本。</small>');
    });
  }
}

function versionActionParameters(toolName, version) {
  const base = {
    project_id: state.versionTree.project_id,
    version_id: version.version_id,
    expected_project_version: state.versionTree.project_version,
    idempotency_key: `ui-version-${toolName}-${Date.now()}`,
  };
  if (toolName === "publish_version") {
    base.review_id = version.governance.latest_review.review_id;
  }
  return base;


}

async function prepareVersionAction(toolName) {
  const contextKey = workbenchContext();
  const request = state.prepareVersionActionRequest = (state.prepareVersionActionRequest || 0) + 1;
  const active = () => workbenchContextActive(contextKey) && request === state.prepareVersionActionRequest;
  if (!active()) return;
  const versions = flattenVersionTree(state.versionTree?.roots || []);
  const version = versions.find((item) => item.version_id === elements.versionGovernanceSelect.value);
  if (!version) return;
  const parameters = versionActionParameters(toolName, version);
  try {
    if (await restoreManualRequest("version", contextKey)) return;
    if (!active()) return;
    const prepared = await api("/api/manual/versions/actions/prepare", {
      method: "POST",
      body: JSON.stringify({
        tool_name: toolName,
        parameters,
      }),
    });
    if (!active()) return;
    showVersionConfirmation({ toolName, parameters, prepared, contextKey });
  } catch (error) {
    if (!active()) return;
    showToast(error.message);
  }
}

function showVersionConfirmation(pending) {
  const { toolName, prepared } = pending;
    state.pendingVersionAction = pending;
    elements.versionConfirmExecute.textContent = "确认执行";
    const titles = {
      record_version_review: "登记已验证人工结论",
      select_candidate_version: "设为当前项目候选",
      publish_version: "创建正式发布登记",
    };
    elements.versionConfirmRisk.textContent = "需要再次确认";
    elements.versionConfirmTitle.textContent = titles[toolName];
    elements.versionConfirmCopy.textContent = `会改变：${prepared.will_change}。不会改变：${prepared.will_not_change}。模型调用：${prepared.calls_models ? "会调用" : "0 次"}。影响范围：${prepared.impact_scope}。`;
    elements.versionConfirmExpiry.textContent = `确认到期：${String(prepared.expires_at || "").replace("T", " ")}`;
    elements.versionConfirmCheck.checked = false;
    elements.versionConfirmExecute.disabled = true;
    elements.versionConfirmation.hidden = false;
    elements.versionConfirmation.scrollIntoView({ behavior: "smooth", block: "center" });
}

function cancelVersionConfirmation() {
  state.pendingVersionAction = null;
  elements.versionConfirmCheck.checked = false;
  elements.versionConfirmation.hidden = true;
}

async function executeVersionAction() {
  const contextKey = workbenchContext();
  const active = () => workbenchContextActive(contextKey);
  if (!active()) return;
  const pending = state.pendingVersionAction;
  if (!pending || !workbenchContextActive(pending.contextKey) || !elements.versionConfirmCheck.checked) return;
  elements.versionConfirmExecute.disabled = true;
  try {
    await retainManualRequest("version", pending);
    if (!active()) return;
    const result = await api("/api/manual/versions/actions/execute", {
      method: "POST",
      body: JSON.stringify({
        tool_name: pending.toolName,
        parameters: pending.parameters,
        command_token: pending.prepared.command_token,
      }),
    });
    if (result.status === "failed") throw new Error(result.error?.message || "版本治理动作未完成");
    const actionResult = result?.result && typeof result.result === "object" ? result.result : result;
    const projectionPending = pending.toolName === "publish_version" && publicationProjectionStatus(actionResult) !== "published";
    if (!projectionPending) completeManualRequest(pending);
    if (!active()) return;
    const notice = pending.toolName === "publish_version"
      ? publicationProjectionNotice(publicationProjectionStatus(actionResult), actionResult)
      : "版本操作已完成，当前状态已更新。";
    cancelVersionConfirmation();
    await loadVersionWorkbench();
    showToast(notice);
  } catch (error) {
    if (!active()) return;
    if (manualRequestDefinitelyRejected(error)) {
      completeManualRequest(pending);
      cancelVersionConfirmation();
      showToast(`本次操作未写入：${error.message}。读取最新状态后可重新准备。`);
      await loadVersionWorkbench();
      return;
    }
    elements.versionConfirmExecute.disabled = false;
    elements.versionConfirmExecute.textContent = "恢复原操作结果";
    elements.versionConfirmExpiry.textContent = `原请求已保留。${error.message}。可用原请求核对结果；不要重复创建同一操作。`;
    showToast(error.message);
  }
}

async function compareWorkbenchVersions() {
  const contextKey = workbenchContext();
  const request = state.compareWorkbenchVersionsRequest = (state.compareWorkbenchVersionsRequest || 0) + 1;
  const active = () => workbenchContextActive(contextKey) && request === state.compareWorkbenchVersionsRequest;
  if (!active()) return;
  const projectId = elements.versionProjectSelect.value;
  const leftVersionId = elements.versionLeft.value;
  const rightVersionId = elements.versionRight.value;
  if (!projectId || !leftVersionId || !rightVersionId) return;
  if (leftVersionId === rightVersionId) {
    showToast("请选择两个不同版本");
    return;
  }
  elements.versionCompare.disabled = true;
  elements.versionDiff.innerHTML = `<div class="workbench-empty">正在核对两个已保存版本…</div>`;
  try {
    const diff = await api("/api/agent/workbench/versions/compare", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        left_version_id: leftVersionId,
        right_version_id: rightVersionId,
      }),
    });
    if (!active()) return;
    renderVersionDiff(diff);
  } catch (error) {
    if (!active()) return;
    elements.versionDiff.innerHTML = `<div class="workbench-empty">${escapeHtml(error.message)}</div>`;
    showToast(error.message);
  } finally {
    if (!active()) return;
    elements.versionCompare.disabled = false;
  }
}

function displayDiffValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 180 ? `${text.slice(0, 177)}…` : text;
}

function renderVersionDiff(diff) {
  const summary = diff.summary || {};
  const relationLabels = { preserved: "保留", modified: "修改", added: "新增", deleted: "删除", split: "拆分", merged: "合并" };
  const relationCounts = Object.entries(summary.relation_counts || {}).map(([name, count]) => `<span>${escapeHtml(relationLabels[name] || name)} <b>${escapeHtml(count)}</b></span>`).join("");
  const changedShots = (diff.shot_diffs || []).filter((shot) =>
    shot.relation !== "preserved" || shot.position_changed || (shot.field_diffs || []).some((field) => field.status !== "unchanged")
  );
  const shotCards = changedShots.map((shot) => {
    const fields = (shot.field_diffs || []).filter((field) => field.status !== "unchanged");
    return `<article class="shot-diff-card">
      <header><span>${escapeHtml(relationLabels[shot.relation] || shot.relation)}</span><strong>镜 ${escapeHtml(shot.parent_shot_nos?.join(", ") || "新增")} → ${escapeHtml(shot.child_shot_no || "删除")}</strong><small>${fields.length} 个字段变化</small></header>
      <div>${fields.map((field) => `<dl><dt>${escapeHtml(formalBusinessFieldLabel(field.field))}</dt><dd><del>${escapeHtml(displayDiffValue(field.before))}</del><ins>${escapeHtml(displayDiffValue(field.after))}</ins></dd></dl>`).join("") || `<p>仅镜头位置发生变化。</p>`}</div>
    </article>`;
  }).join("");
  elements.versionDiff.innerHTML = `
    <div class="diff-metrics"><span><b>${escapeHtml(summary.parent_shot_count || 0)}</b>基准镜数</span><span><b>${escapeHtml(summary.child_shot_count || 0)}</b>修改镜数</span><span><b>${escapeHtml(summary.changed_shot_count || 0)}</b>变化镜头</span><span><b>${escapeHtml(summary.changed_field_count || 0)}</b>变化字段</span></div>
    <div class="relation-strip">${relationCounts || "<span>没有关系变化</span>"}</div>
    ${diff.lineage_needs_review ? `<div class="diff-warning">镜头对应关系存在歧义，需要人工确认。</div>` : `<div class="diff-evidence">两个版本的已保存内容均已校验。</div>`}
    <div class="shot-diff-list">${shotCards || `<div class="workbench-empty compact-empty">两个版本没有可见字段差异。</div>`}</div>`;
}

async function loadAssetWorkbench() {
  if (!workbenchReady(elements.assetIdentityNote, elements.assetDesk)) return;
  const projectId = elements.assetProjectSelect.value;
  const contextKey = workbenchContext(), request = ++state.assetRequest;
  const active = () => workbenchContextActive(contextKey) && request === state.assetRequest;
  if (!projectId) return;
  elements.assetTemplateLink.href = formalPath("brief", projectId, { panel: "templates" });
  elements.assetRefresh.disabled = true;
  elements.assetSearch.disabled = true;
  state.assetContext = null;
  elements.assetPackageLabel.textContent = "正在读取资源…";
  elements.assetImports.innerHTML = '<div class="workbench-empty">正在读取资产记录…</div>';
  try {
    const session = await ensureAgentProjectSession(projectId);
    if (!active()) return;
    const sessionId = session.session.agent_session_id;
    state.workbenchSessionId = sessionId;
    const context = await api(`/api/agent/workbench/assets?agent_session_id=${encodeURIComponent(sessionId)}`);
    if (!active()) return;
    state.assetContext = context;
    elements.assetPackageLabel.textContent = `资源版本 ${context.package_version}`;
    renderAssetImports(context.imports || []);
    renderFrozenAssets();
    await restoreManualRequest("asset", workbenchContext());
  } catch (error) {
    if (!active()) return;
    state.assetContext = null;
    elements.assetPackageLabel.textContent = "资源读取失败";
    elements.assetImports.innerHTML = `<div class="workbench-empty"><strong>资产读取失败</strong><p>${escapeHtml(formalError(error))}</p><button class="secondary-button compact" data-assets-retry>重新读取</button></div>`;
  } finally {
    if (active()) {
      elements.assetRefresh.disabled = false;
      elements.assetSearch.disabled = !state.assetContext;
    }
  }
}

function chooseAssetFile() {
  elements.assetUploadFile.click();
}

async function freezeAssetFile() {
  const contextKey = workbenchContext();
  const active = () => workbenchContextActive(contextKey);
  if (!active()) return;
  const file = elements.assetUploadFile.files[0];
  if (!file || !contextKey.sessionId) return;
  elements.assetFreeze.disabled = true;
  try {
    const body = new FormData();
    body.append("file", file);
    const frozen = await api(`/api/agent/workbench/assets/upload?agent_session_id=${encodeURIComponent(contextKey.sessionId)}`, { method: "POST", body });
    if (!active()) return;
    if (!state.frozenAssets.some((item) => item.content_sha256 === frozen.content_sha256 && item.kind === frozen.kind)) {
      state.frozenAssets.push(frozen);
    }
    if (elements.assetUploadFile.files[0] === file) {
      elements.assetLocalPath.value = ""; elements.assetUploadFile.value = "";
    }
    renderFrozenAssets();
    showToast("文件已加入待导入列表；不会公开本机来源路径。");
  } catch (error) {
    if (!active()) return;
    showToast(error.message);
  } finally {
    if (!active()) return;
    elements.assetFreeze.disabled = !elements.assetLocalPath.value.trim();
  }
}

function renderFrozenAssets() {
  elements.assetFrozenList.innerHTML = state.frozenAssets.length ? state.frozenAssets.map((item, index) => `
    <div class="frozen-asset"><span>${escapeHtml({image:"图片",workbook:"Excel",script:"脚本"}[item.kind] || "文件")}</span><div><strong>${escapeHtml(item.basename)}</strong><small>等待确认导入</small></div><button type="button" data-remove-frozen="${index}" aria-label="移除">×</button></div>
  `).join("") : `<div class="workbench-empty compact-empty">尚未选择文件</div>`;
  elements.assetStage.disabled = state.frozenAssets.length === 0 || !state.assetContext;
}

function assetIdempotency(action) {
  const random = globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2);
  return `ui_${action}_${Date.now()}_${random}`;
}

async function prepareAssetAction(toolName, record = null) {
  const contextKey = workbenchContext();
  const request = state.prepareAssetActionRequest = (state.prepareAssetActionRequest || 0) + 1;
  const active = () => workbenchContextActive(contextKey) && request === state.prepareAssetActionRequest;
  if (!active()) return;
  const context = state.assetContext;
  if (!context || !contextKey.sessionId) return;
  const common = {
    package_id: context.package_id,
    expected_active_package_version: context.package_version,
    idempotency_key: assetIdempotency(toolName),
  };
  const parameters = toolName === "create_asset_import" ? {
    ...common,
    category_id: context.category_id,
    scope: elements.assetScope.value,
    file_refs: state.frozenAssets.map((item) => item.artifact_ref),
  } : {
    ...common,
    import_id: record.import_id,
    expected_import_revision: record.revision,
  };
  try {
    const manualPublish = toolName === "publish_asset_import";
    if (manualPublish && await restoreManualRequest("asset", contextKey)) return;
    const confirmation = await api(manualPublish ? "/api/manual/assets/publish/prepare" : "/api/agent/workbench/assets/actions/prepare", {
      method: "POST",
      body: JSON.stringify(manualPublish
        ? { project_id: context.project_id, ...parameters }
        : { agent_session_id: contextKey.sessionId, tool_name: toolName, parameters }),
    });
    if (!active()) return;
    state.pendingAssetAction = { toolName, parameters, confirmation, contextKey,
      scope: confirmation.target_summary?.scope || record?.scope || parameters.scope };
    renderAssetConfirmation();
  } catch (error) {
    if (!active()) return;
    showToast(error.message);
  }
}

function renderAssetConfirmation() {
  const pending = state.pendingAssetAction;
  if (!pending) {
    elements.assetConfirmation.hidden = true;
    return;
  }
  const titles = {
    create_asset_import: "确认创建导入记录",
    review_asset_import: "确认人工审核通过",
    publish_asset_import: "确认发布登记",
  };
  const changes = {
    create_asset_import: "写入一条待审核导入记录；检查通过不代表人工审核通过。",
    review_asset_import: "记录本次人工审核结论。",
    publish_asset_import: "发布为可检索资产；使用版本化资产包时，会更新同类型使用的资产包版本。",
  };
  const scope = pending.toolName === "create_asset_import"
    ? (pending.parameters.scope === "shared" ? "同类型共享资产范围" : "当前项目资产范围")
    : pending.scope === "shared" ? "同类型共享资产范围" : "当前项目资产范围";
  elements.assetConfirmRisk.textContent = "请核对";
  elements.assetConfirmTitle.textContent = titles[pending.toolName];
  const evidence = pending.confirmation.target_summary?.items || [];
  const names = evidence.map(item => item.filename).slice(0, 3).join("、");
  const target = names ? `发布对象：${names}${evidence.length > 3 ? `等 ${evidence.length} 个文件` : ""}。` : "";
  elements.assetConfirmCopy.textContent = `${target}会改变：${changes[pending.toolName]} 不会改变：${pending.confirmation.will_not_change}。影响范围：${scope}。`;
  let sourceEvidence = document.querySelector("#asset-confirm-evidence");
  if (!sourceEvidence) {
    sourceEvidence = document.createElement("details"); sourceEvidence.id = "asset-confirm-evidence";
    sourceEvidence.style.overflowWrap = "anywhere";
    elements.assetConfirmCopy.after(sourceEvidence);
  }
  sourceEvidence.hidden = !evidence.length;
  sourceEvidence.open = false;
  sourceEvidence.innerHTML = `<summary>核对原文件</summary>${evidence.map(item => `<p>${escapeHtml(item.filename)} · ${escapeHtml({image: "图片", workbook: "表格"}[item.asset_kind] || item.asset_kind)}<br><small>SHA-256：${escapeHtml(item.content_sha256)}</small></p>`).join("")}`;
  elements.assetConfirmExpiry.textContent = pending.confirmation.expires_at
    ? `确认到期：${String(pending.confirmation.expires_at).replace("T", " ")}` : "确认前请核对以上对象与范围。";
  elements.assetConfirmCheck.checked = false;
  elements.assetConfirmExecute.textContent = "确认执行";
  elements.assetConfirmExecute.disabled = true;
  elements.assetConfirmation.hidden = false;
  elements.assetConfirmation.scrollIntoView({ behavior: "smooth", block: "center" });
}

function cancelAssetConfirmation() {
  state.pendingAssetAction = null;
  elements.assetConfirmCheck.checked = false;
  elements.assetConfirmation.hidden = true;
}

async function executeAssetAction() {
  const contextKey = workbenchContext();
  const active = () => workbenchContextActive(contextKey);
  if (!active()) return;
  const pending = state.pendingAssetAction;
  if (!pending || !workbenchContextActive(pending.contextKey) || !elements.assetConfirmCheck.checked) return;
  elements.assetConfirmExecute.disabled = true;
  try {
    const manualPublish = pending.toolName === "publish_asset_import";
    const recoverable = manualPublish || pending.toolName === "create_asset_import";
    if (!manualPublish) pending.originalAgentSessionId ||= contextKey.sessionId;
    if (recoverable) await retainManualRequest("asset", pending);
    const result = await api(manualPublish ? "/api/manual/assets/publish/execute" : "/api/agent/workbench/assets/actions/execute", {
      method: "POST",
      body: JSON.stringify(manualPublish
        ? { project_id: pending.contextKey.projectId, parameters: pending.parameters, command_token: pending.confirmation.command_token }
        : { agent_session_id: pending.originalAgentSessionId || contextKey.sessionId, tool_name: pending.toolName, parameters: pending.parameters, confirmation_token: pending.confirmation.confirmation_token }),
    });
    if (result.status !== "completed") throw new Error(result.error?.message || "资产动作未完成");
    if (recoverable) completeManualRequest(pending);
    if (!active()) return;
    if (pending.toolName === "create_asset_import") state.frozenAssets = [];
    cancelAssetConfirmation();
    renderFrozenAssets();
    await loadAssetWorkbench();
    showToast(result.replayed ? "已恢复同一资产操作结果" : "资产操作已完成并记录审计");
  } catch (error) {
    if (!active()) return;
    elements.assetConfirmExecute.disabled = false;
    if (pending.recoveryKey) {
      elements.assetConfirmRisk.textContent = "操作结果待核对";
      elements.assetConfirmExpiry.textContent = "原请求已保留；再次确认会恢复同一操作结果。关闭面板不会撤销已经执行的操作。";
      elements.assetConfirmExecute.textContent = "恢复原操作结果";
    }
    showToast(error.message);
  }
}

function renderAssetImports(imports) {
  elements.assetImports.innerHTML = imports.length ? imports.map((record) => {
    const canReview = record.state === "staged" && record.machine_review_status === "passed";
    const canPublish = record.state === "reviewed";
    const items = (record.items || []).map((item) => `<span>${escapeHtml(item.filename)} · ${escapeHtml({passed:"检查通过",available:"可用",failed:"检查未通过",pending:"等待检查"}[item.validation_status] || "待核对")}</span>`).join("");
    const stateLabel = { staged: "待审核", reviewed: "已审核", published: "已登记" }[record.state] || record.state;
    const checkLabel = record.machine_review_status === "passed" ? "文件检查通过" : "文件检查待处理";
    return `<article class="import-card" data-import-id="${escapeHtml(record.import_id)}" tabindex="-1">
      <header><span class="state-pill asset-state-${escapeHtml(record.state)}">${escapeHtml(stateLabel)}</span><div><strong>导入记录</strong><small>${record.scope === "shared" ? "同类型共享" : "仅当前项目"} · 第 ${escapeHtml(record.revision)} 版</small></div><b>${escapeHtml(checkLabel)}</b></header>
      <div class="import-items">${items}</div>
      <footer>
        ${canReview ? `<button class="secondary-button compact" type="button" data-asset-review="${escapeHtml(record.import_id)}">检查人工审核影响</button>` : ""}
        ${canPublish ? `<button class="primary-button compact" type="button" data-asset-publish="${escapeHtml(record.import_id)}">检查发布登记</button>` : ""}
        ${record.state === "staged" ? `<small>仍需人工审核</small>` : record.state === "published" ? `<small>已可在当前范围检索</small>` : `<small>已人工审核，待登记</small>`}
      </footer>
    </article>`;
  }).join("") : `<div class="workbench-empty">当前项目还没有导入记录。</div>`;
  focusWorkbenchObject("import_id", "data-import-id", elements.assetImports);
}

async function searchWorkbenchAssets() {
  const contextKey = workbenchContext();
  const request = state.searchWorkbenchAssetsRequest = (state.searchWorkbenchAssetsRequest || 0) + 1;
  const active = () => workbenchContextActive(contextKey) && request === state.searchWorkbenchAssetsRequest;
  if (!active()) return;
  const query = elements.assetSearchQuery.value.trim();
  if (!query || !state.assetContext || !contextKey.sessionId) return;
  elements.assetSearch.disabled = true;
  try {
    const result = await api("/api/agent/workbench/assets/search", {
      method: "POST",
      body: JSON.stringify({
        agent_session_id: contextKey.sessionId,
        category_id: state.assetContext.category_id,
        query,
        kind: elements.assetSearchKind.value,
      }),
    });
    if (!active()) return;
    const items = result.results || [];
    elements.assetSearchResults.innerHTML = items.length ? items.map((item) => {
      const source = item.source || {};
      const kind = { image: "图片", workbook: "Excel", script: "原资产包脚本" }[item.asset_kind] || "已登记文件";
      const scope = { project: "仅当前项目", shared: "同类型共享" }[source.scope] || "原资产包";
      const status = { published: "已登记", reviewed: "已审核，待登记", staged: "待审核" }[item.workflow_state] || (item.workflow_state ? "登记状态待核对" : "原资产包内容");
      const location = [source.source_file, source.source_sheet, source.source_row != null ? `第 ${source.source_row} 行` : ""].filter(Boolean).join(" · ");
      return `<article><span>${escapeHtml(kind)}</span><strong>${escapeHtml(item.title || "未命名资产")}</strong><small>${escapeHtml(scope)} · ${escapeHtml(status)}</small><details><summary>查看来源</summary><p>${escapeHtml(location || "当前记录未提供来源位置。")}</p></details></article>`;
    }).join("") : `<div class="workbench-empty compact-empty">没有匹配的已登记资产。请调整关键词或类型。</div>`;
  } catch (error) {
    if (!active()) return;
    elements.assetSearchResults.innerHTML = `<div class="workbench-empty" role="status">资产检索失败：${escapeHtml(formalError(error))} 搜索条件已保留，可重新检索。</div>`;
  } finally {
    if (!active()) return;
    elements.assetSearch.disabled = false;
  }
}

const governanceScopeLabels = {
  company: "公司",
  category: "类别",
  project: "项目",
  personal: "个人",
};

function governanceIdempotency(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

async function loadGovernanceWorkbench() {
  if (!workbenchReady(elements.governanceIdentityNote, elements.governanceDesk)) return;
  const contextKey = workbenchContext(), request = ++state.governanceRequest;
  const active = () => workbenchContextActive(contextKey) && request === state.governanceRequest;
  const projectId = elements.governanceProjectSelect.value;
  if (!projectId) return;
  cancelGovernanceConfirmation();
  elements.governanceRefresh.disabled = true;
  elements.strategyReadiness.className = "strategy-readiness";
  elements.strategyReadiness.innerHTML = `<b>读取中</b><small>核对已确认需求与创作规范</small>`;
  elements.strategyGrid.innerHTML = `<div class="workbench-empty">正在整理适合当前项目的创作方向…</div>`;
  try {
    const session = await ensureAgentProjectSession(projectId);
    if (!active()) return;
    state.workbenchSessionId = session.session.agent_session_id;
    const strategyPath = `/api/agent/governance/strategies?agent_session_id=${encodeURIComponent(state.workbenchSessionId)}`;
    const knowledgePath = `/api/agent/governance/knowledge?agent_session_id=${encodeURIComponent(state.workbenchSessionId)}`;
    const [strategyResult, knowledgeResult, evidenceResult] = await Promise.allSettled([
      api(strategyPath),
      api(knowledgePath),
      api(`/api/agent/governance/knowledge/evidence?agent_session_id=${encodeURIComponent(state.workbenchSessionId)}`),
    ]);
    if (!active()) return;
    if (strategyResult.status === "fulfilled") {
      state.strategyContext = strategyResult.value;
      renderStrategyContext(strategyResult.value);
    } else {
      state.strategyContext = null;
      elements.strategyReadiness.classList.add("needs-clarification");
      elements.strategyReadiness.innerHTML = `<b>暂不可用</b><small>策略资料需要核对</small>`;
      elements.strategyGrid.innerHTML = `<div class="workbench-empty">${escapeHtml(strategyResult.reason.message)}<p><a class="secondary-button compact" href="${formalPath("brief", projectId)}" data-formal-target>核对项目需求</a></p></div>`;
    }
    const selectedEvidence = new Set(Array.from(elements.knowledgeEvidence.selectedOptions).map(option => option.value));
    if (evidenceResult.status === "fulfilled") {
      const options = evidenceResult.value.items || [];
      elements.knowledgeEvidence.innerHTML = options.map(item => `<option value="${escapeHtml(item.evidence_id)}"${item.available ? "" : " disabled"}${selectedEvidence.has(item.evidence_id) ? " selected" : ""}>${escapeHtml(item.label)} · ${escapeHtml(taskDisplayTime(item.created_at))}${item.available ? "" : " · 已失效"}</option>`).join("");
      document.querySelector("#knowledge-evidence-note").textContent = options.some(item => item.available) ? "选择规则所依据的资料，可多选。提交和确认时会重新核对归属。" : "暂无可用证据，请先到项目需求页导入并确认资料，或保存项目版本。";
    } else {
      elements.knowledgeEvidence.innerHTML = "";
      document.querySelector("#knowledge-evidence-note").textContent = `证据读取失败：${formalError(evidenceResult.reason)}。请重新读取规则页。`;
    }
    if (knowledgeResult.status === "fulfilled") {
      state.knowledgeContext = knowledgeResult.value;
      renderKnowledgeContext(knowledgeResult.value);
    } else {
      state.knowledgeContext = null;
      elements.knowledgeRules.innerHTML = `<div class="workbench-empty">${escapeHtml(knowledgeResult.reason.message)}</div>`;
      elements.pendingKnowledge.innerHTML = `<div class="workbench-empty">知识候选暂时不可读取。</div>`;
    }
    await restoreManualRequest("knowledge", contextKey);
  } catch (error) {
    if (active()) showToast(formalError(error));
  } finally {
    if (active()) elements.governanceRefresh.disabled = false;
  }
}

async function analyzeGovernanceStrategies() {
  const contextKey = workbenchContext();
  if (!workbenchContextActive(contextKey) || !contextKey.sessionId || state.strategyAnalyzing) return;
  state.strategyAnalyzing = true;
  const button = elements.strategyGrid.querySelector("[data-strategy-analyze]");
  if (button) { button.disabled = true; button.textContent = "正在分析…"; }
  try {
    const result = await api("/api/agent/governance/strategies/analyze", {
      method: "POST", body: JSON.stringify({ agent_session_id: contextKey.sessionId }),
    });
    if (!workbenchContextActive(contextKey)) return;
    state.strategyContext = result;
    renderStrategyContext(result);
  } catch (error) {
    if (workbenchContextActive(contextKey)) showToast(formalError(error));
  } finally {
    state.strategyAnalyzing = false;
    if (button?.isConnected) { button.disabled = false; button.textContent = "分析当前需求"; }
  }
}

function renderStrategyContext(context) {
  const portfolio = context.portfolio;
  if (!portfolio) {
    const label = { not_analyzed: "尚未分析", stale: "需求或依据已变化", analyzing: "分析进行中" }[context.status] || "暂不可用";
    const message = { not_analyzed: "点击分析当前需求，生成可比较的创作方向。浏览页面不会自动计算。",
      stale: "原分析结果已保留；分析当前需求后再选择方向。",
      analyzing: "原任务仍在分析，请稍后点击重新读取查看结果。" }[context.status] || context.error?.message || "当前策略证据不可用，请查看原任务或核对项目需求。";
    elements.strategyReadiness.classList.add("needs-clarification");
    elements.strategyReadiness.innerHTML = `<b>${escapeHtml(label)}</b>`;
    elements.strategyClarification.hidden = true;
    elements.strategyGrid.innerHTML = `<div class="workbench-empty"><p>${escapeHtml(message)}</p>${["not_analyzed", "stale"].includes(context.status) ? '<button class="primary-button compact" type="button" data-strategy-analyze>分析当前需求</button>' : ""}<p><a class="secondary-button compact" href="${formalPath("brief", state.workbenchProjectId)}" data-formal-target>核对项目需求</a></p></div>`;
    return;
  }
  const coverage = portfolio.coverage || {};
  const ready = Boolean(coverage.comparison_ready);
  elements.strategyReadiness.classList.toggle("needs-clarification", !ready);
  elements.strategyReadiness.innerHTML = `<b>${ready ? `${coverage.selectable_direction_count} 个方向` : "需补资料"}</b><small>${ready ? `预计 ${portfolio.baseline?.planned_shot_count || 0} 镜 · 可选择` : "至少需要 2 个有证据方向"}</small>`;
  const directions = portfolio.directions || [];
  elements.strategyGrid.innerHTML = directions.length ? directions.map((direction, index) => strategyCard(direction, ready, index)).join("") : `<div class="workbench-empty">没有生成任何策略方向。</div>`;
  const reasons = [
    ...(portfolio.readiness_reasons || []).map((item) => item.message),
    ...(portfolio.unsupported_directions || []).map((item) => item.message),
  ];
  elements.strategyClarification.hidden = reasons.length === 0;
  elements.strategyClarification.innerHTML = `${escapeHtml(reasons.join(" "))}${!ready ? `<p><a class="secondary-button compact" href="${formalPath("brief", state.workbenchProjectId)}" data-formal-target>补充项目需求</a></p>` : ""}`;
}

function strategyCard(direction, comparisonReady, index) {
  const emphasisIds = new Set((direction.emphasis || []).map((item) => item.baseline_event_id));
  const events = (direction.event_order || []).map((event) => `
    <div class="strategy-event ${emphasisIds.has(event.baseline_event_id) ? "is-emphasis" : ""}"><b>${String(event.position).padStart(2, "0")}</b><span>${escapeHtml(event.title)}${emphasisIds.has(event.baseline_event_id) ? " · 重点" : ""}</span></div>
  `).join("");
  const risks = (direction.risks_and_confirmations || []).filter((item) => item.status !== "info");
  const change = (direction.baseline_diff?.will_change || []).map(governanceContractLabel).join("、") || "无";
  const unchanged = (direction.baseline_diff?.will_not_change || []).map(governanceContractLabel).join("、");
  const selectable = comparisonReady && direction.selectable;
  return `<article class="strategy-card ${selectable ? "" : "unselectable"}">
    <header><span>方向 ${index + 1}</span><h3>${escapeHtml(direction.name)}</h3><p>${escapeHtml(direction.narrative_principle)}</p></header>
    <div class="strategy-events">${events}</div>
    <div class="strategy-stats"><span><b>${escapeHtml(direction.planned_shot_count)}</b>预计镜数</span><span><b>${escapeHtml(direction.duration_budget?.target_seconds || 0)}s</b>目标时长</span></div>
    <div class="strategy-contract"><div><strong>会改变</strong><p>${escapeHtml(change)}</p></div><div><strong>不会改变</strong><p>${escapeHtml(unchanged)}</p></div>${risks.length ? `<div class="strategy-risk">风险 / 确认：${escapeHtml(risks.map((item) => item.message).join("；"))}</div>` : `<div class="strategy-risk">规划检查通过；生成后仍需人工审核。</div>`}</div>
    <footer><small>本次不调用模型 · ${selectable ? "可选择" : "需补充依据"}</small>${selectable ? `<button class="primary-button compact" type="button" data-strategy-select="${escapeHtml(direction.strategy_id)}">准备选择</button>` : `<small>不可选择</small>`}</footer>
  </article>`;
}

function governanceContractLabel(value) {
  const labels = {
    event_order: "事件顺序",
    event_emphasis: "事件重点",
    narrative_principle_only: "叙事原则",
    confirmed_brief_facts: "已确认的需求信息",
    event_set: "事件集合",
    planned_shot_count: "镜数 N",
    target_duration: "目标时长",
    brief_evidence: "Brief 证据",
    functional_source_triples: "来源三元组",
    publication_state: "发布状态",
  };
  return labels[value] || value;
}

async function prepareStrategySelection(strategyId) {
  const contextKey = workbenchContext();
  const request = state.prepareStrategySelectionRequest = (state.prepareStrategySelectionRequest || 0) + 1;
  const active = () => workbenchContextActive(contextKey) && request === state.prepareStrategySelectionRequest;
  if (!active()) return;
  const context = state.strategyContext;
  if (!context?.portfolio_ref || !contextKey.sessionId) return;
  try {
    const prepared = await api("/api/agent/governance/strategies/prepare", {
      method: "POST",
      body: JSON.stringify({
        agent_session_id: contextKey.sessionId,
        strategy_portfolio_ref: context.portfolio_ref,
        strategy_id: strategyId,
        idempotency_key: governanceIdempotency("ui-strategy"),
      }),
    });
    if (!active()) return;
    showGovernanceConfirmation({
      contextKey, prepared,
      title: `选择“${(context.portfolio.directions || []).find((item) => item.strategy_id === strategyId)?.name || strategyId}”`,
      executePath: "/api/agent/governance/strategies/execute",
      kind: "strategy",
    });
  } catch (error) {
    if (!active()) return;
    showToast(error.message);
  }
}

function renderKnowledgeContext(context) {
  elements.knowledgeVersion.textContent = `v${context.knowledge_version}`;
  elements.knowledgeRole.textContent = context.can_approve ? `${context.actor_display_name} · 管理员可审批` : `${context.actor_display_name} · 可提交候选`;
  const rules = context.effective_rules || [];
  elements.knowledgeRules.innerHTML = (context.precedence || ["company", "category", "project", "personal"]).map((scope) => {
    const scoped = rules.filter((rule) => rule.scope === scope);
    return `<section class="rule-scope"><header><strong>${governanceScopeLabels[scope]}</strong><span>${scoped.length} 条有效</span></header>${scoped.length ? scoped.map((rule) => `<article class="rule-card"><p>${escapeHtml(rule.statement)}</p><small>第 ${escapeHtml(rule.active_version)} 版 · ${escapeHtml(`${rule.evidence_refs.length} 项已登记依据资料`)}</small></article>`).join("") : `<div class="rule-empty">本层暂无有效规则</div>`}</section>`;
  }).join("");
  const drafts = new Map([...elements.pendingKnowledge.querySelectorAll("[data-approval-evidence]")].map(field => [field.dataset.approvalEvidence, field.value]));
  const pending = context.pending_candidates || [];
  elements.pendingKnowledgeCount.textContent = `${pending.length} 条建议`;
  elements.pendingKnowledge.innerHTML = pending.length ? pending.map((candidate) => pendingKnowledgeCard(candidate, context.can_approve)).join("") : `<div class="workbench-empty compact-empty">当前可见范围没有待审批候选。</div>`;
  elements.pendingKnowledge.querySelectorAll("[data-approval-evidence]").forEach(field => { field.value = drafts.get(field.dataset.approvalEvidence) || ""; });
  focusWorkbenchObject("candidate_id", "data-candidate-id", elements.pendingKnowledge);
}

function pendingKnowledgeCard(candidate, canApprove) {
  const evidence = `${(candidate.evidence_refs || []).length} 项已登记依据资料`;
  const controls = canApprove ? `<div class="pending-approval-controls"><textarea data-approval-evidence="${escapeHtml(candidate.candidate_id)}" placeholder="填写管理员核对证据与结论"></textarea><button class="primary-button compact" type="button" data-knowledge-approve="${escapeHtml(candidate.candidate_id)}">检查批准影响</button></div>` : `<div class="pending-awaiting-admin">等待管理员审核。当前身份没有审批权限，因此不显示审批按钮。</div>`;
  return `<article class="pending-knowledge-card" data-candidate-id="${escapeHtml(candidate.candidate_id)}" tabindex="-1"><span>待审核</span><div><h3>${escapeHtml(candidate.statement)}</h3><p>${escapeHtml(governanceScopeLabels[candidate.scope])}范围 · 第 ${escapeHtml(candidate.knowledge_version)} 版<br />证据：${escapeHtml(evidence)}</p></div>${controls}</article>`;
}

async function prepareKnowledgeCandidate() {
  const contextKey = workbenchContext();
  const request = state.prepareKnowledgeCandidateRequest = (state.prepareKnowledgeCandidateRequest || 0) + 1;
  const active = () => workbenchContextActive(contextKey) && request === state.prepareKnowledgeCandidateRequest;
  if (!active()) return;
  const statement = elements.knowledgeStatement.value.trim();
  const evidenceIds = Array.from(elements.knowledgeEvidence.selectedOptions).map(option => option.value);
  if (!statement) {
    showToast("请填写规则陈述");
    elements.knowledgeStatement.focus();
    return;
  }
  if (!evidenceIds.length) {
    showToast("请选择至少一项依据资料");
    elements.knowledgeEvidence.focus();
    return;
  }
  elements.knowledgePropose.disabled = true;
  try {
    const prepared = await api("/api/agent/governance/knowledge/candidates/prepare", {
      method: "POST",
      body: JSON.stringify({
        agent_session_id: contextKey.sessionId,
        scope: elements.knowledgeScope.value,
        statement,
        evidence_ids: evidenceIds,
        idempotency_key: governanceIdempotency("ui-knowledge-candidate"),
      }),
    });
    if (!active()) return;
    showGovernanceConfirmation({
      contextKey, prepared,
      title: "提交规则建议",
      executePath: "/api/agent/governance/knowledge/candidates/execute",
      kind: "knowledge-candidate",
    });
  } catch (error) {
    if (!active()) return;
    showToast(error.message);
  } finally {
    if (!active()) return;
    elements.knowledgePropose.disabled = false;
  }
}

async function prepareKnowledgeApproval(candidateId) {
  const contextKey = workbenchContext();
  const request = state.prepareKnowledgeApprovalRequest = (state.prepareKnowledgeApprovalRequest || 0) + 1;
  const active = () => workbenchContextActive(contextKey) && request === state.prepareKnowledgeApprovalRequest;
  if (!active()) return;
  if (!state.knowledgeContext?.can_approve) return;
  const field = elements.pendingKnowledge.querySelector(`[data-approval-evidence="${CSS.escape(candidateId)}"]`);
  const reviewEvidence = field?.value.trim() || "";
  if (!reviewEvidence) {
    showToast("请先填写管理员审核证据");
    field?.focus();
    return;
  }
  try {
    if (await restoreManualRequest("knowledge", contextKey)) return;
    if (!active()) return;
    const prepared = await api("/api/manual/knowledge/approvals/prepare", {
      method: "POST",
      body: JSON.stringify({
        project_id: state.knowledgeContext.project_id,
        candidate_id: candidateId,
        review_evidence: reviewEvidence,
        idempotency_key: governanceIdempotency("ui-knowledge-approval"),
      }),
    });
    if (!active()) return;
    showGovernanceConfirmation({
      contextKey, prepared,
      title: "批准规则建议",
      executePath: "/api/manual/knowledge/approvals/execute",
      kind: "knowledge-approval",
    });
  } catch (error) {
    if (!active()) return;
    showToast(error.message);
  }
}

function showGovernanceConfirmation({ contextKey, prepared, title, executePath, kind, recoveryKey }) {
  if (!workbenchContextActive(contextKey)) return;
  state.pendingGovernanceAction = { prepared, title, executePath, kind, contextKey, recoveryKey };
  elements.governanceConfirmExecute.textContent = "确认执行";
  elements.governanceConfirmRisk.textContent = "请核对";
  elements.governanceConfirmTitle.textContent = title;
  elements.governanceWillChange.textContent = prepared.will_change;
  elements.governanceWillNotChange.textContent = prepared.will_not_change;
  elements.governanceModelCalls.textContent = prepared.calls_models ? "会调用" : "0 次";
  elements.governanceImpactScope.textContent = prepared.impact_scope;
  elements.governanceConfirmExpiry.textContent = `确认到期：${String(prepared.expires_at || "").replace("T", " ")}`;
  elements.governanceConfirmCheck.checked = false;
  elements.governanceConfirmExecute.disabled = true;
  elements.governanceConfirmation.hidden = false;
  elements.governanceConfirmation.scrollIntoView({ behavior: "smooth", block: "center" });
}

function cancelGovernanceConfirmation() {
  state.pendingGovernanceAction = null;
  elements.governanceConfirmCheck.checked = false;
  elements.governanceConfirmation.hidden = true;
}

async function executeGovernanceAction() {
  const contextKey = workbenchContext();
  const active = () => workbenchContextActive(contextKey);
  if (!active()) return;
  const pending = state.pendingGovernanceAction;
  if (!pending || !workbenchContextActive(pending.contextKey) || !elements.governanceConfirmCheck.checked) return;
  elements.governanceConfirmExecute.disabled = true;
  try {
    const manualApproval = pending.kind === "knowledge-approval";
    if (manualApproval) await retainManualRequest("knowledge", pending);
    if (!active()) return;
    const result = await api(pending.executePath, {
      method: "POST",
      body: JSON.stringify(manualApproval
        ? { project_id: pending.contextKey.projectId, parameters: pending.prepared.parameters, command_token: pending.prepared.command_token }
        : { agent_session_id: contextKey.sessionId, parameters: pending.prepared.parameters, confirmation_token: pending.prepared.confirmation_token }),
    });
    if (result.status === "failed") throw new Error(result.error?.message || "本次操作未完成");
    if (manualApproval) completeManualRequest(pending);
    if (!active()) return;
    const notice = {
      strategy: "策略已写入派生 Brief，当前项目快照已切换；尚未生成或发布脚本。",
      "knowledge-candidate": "候选已提交，仍处于 pending，不会进入有效规则。",
      "knowledge-approval": "管理员批准完成；规则已按原候选层级生效。",
    }[pending.kind];
    if (pending.kind === "knowledge-candidate") {
      elements.knowledgeStatement.value = "";
      Array.from(elements.knowledgeEvidence.options).forEach(option => { option.selected = false; });
      elements.knowledgeScope.value = "project";
    }
    cancelGovernanceConfirmation();
    await loadGovernanceWorkbench();
    showToast(notice || "本次操作已完成");
  } catch (error) {
    if (!active()) return;
    if (pending.kind === "knowledge-approval" && manualRequestDefinitelyRejected(error)) {
      completeManualRequest(pending);
      cancelGovernanceConfirmation();
      showToast(`本次批准未写入：${error.message}。读取最新状态后可重新准备。`);
      await loadGovernanceWorkbench();
      return;
    }
    elements.governanceConfirmExecute.disabled = false;
    if (pending.kind === "knowledge-approval") {
      elements.governanceConfirmExecute.textContent = "恢复原操作结果";
      elements.governanceConfirmExpiry.textContent = `原请求已保留。${error.message}。可用原请求核对结果；不要重复创建同一批准。`;
    }
    showToast(error.message);
  }
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 15 ? `${text.slice(0, 12)}…` : text;
}

const formalSections = new Set(["overview", "brief", "materials", "plan", "script", "shot-images", "review", "assistant"]);
const workspacePaths = new Map([
  ["/projects", "projects"],
  ["/tasks", "tasks"],
  ["/materials", "materials"],
  ["/shot-images", "shot-images"],
  ["/tools", "tools"],
  ["/assistant", "assistant"],
  ["/assets", "assets"],
  ["/knowledge", "knowledge"],
  ["/system", "system"],
  ["/admin", "admin"],
]);
const formalPageLabels = {
  overview: "概览", brief: "需求", materials: "图库与素材", plan: "结构",
  script: "创作", versions: "版本管理", "shot-images": "逐镜配图",
  review: "审核与交付", assistant: "项目助手",
  admin: "管理员后台",
};

function formalPath(section = "overview", projectId = "", query = {}) {
  if (projectId && typeof projectId === "object") {
    query = projectId;
    projectId = "";
  }
  const currentProjectId = projectId || formalRoute().projectId || "";
  const path = currentProjectId ? `/projects/${encodeURIComponent(currentProjectId)}/${section}` : "/projects";
  const params = new URLSearchParams(query);
  return formalCanonicalPath(params.size ? `${path}?${params.toString()}` : path);
}

function formalNavigationTarget(link) {
  const route = link.dataset.route;
  if (route === "new") return "/projects/new";
  if (!formalSections.has(route)) return link.getAttribute("href") || "/projects";
  const projectId = formalRoute().projectId || (route === "assistant" ? state.lastProjectId : "");
  return projectId ? formalPath(route, projectId) : link.getAttribute("href") || "/projects";
}

function formalCanonicalPath(path) {
  const url = new URL(path, window.location.origin);
  if (url.pathname.endsWith("/script") && url.searchParams.get("aux") === "materials") {
    url.pathname = url.pathname.replace(/\/script$/, "/materials"); url.searchParams.delete("aux"); url.searchParams.delete("panel");
  }
  const match = url.pathname.match(/^\/projects\/([^/]+)\/(plan|shot-images)$/);
  if (!match) return `${url.pathname}${url.search}${url.hash}`;
  url.pathname = `/projects/${match[1]}/script`;
  url.searchParams.set("panel", match[2] === "plan" ? "structure" : "images");
  if (match[2] === "materials") url.searchParams.set("aux", "materials");
  return `${url.pathname}${url.search}${url.hash}`;
}

function formalRoute() {
  const path = window.location.pathname;
  const params = new URLSearchParams(window.location.search);
  if (workspacePaths.has(path)) {
    const page = workspacePaths.get(path);
    if (page === "shot-images") {
      return { page: "script", navPage: "script", displayPage: "script", projectId: params.get("project_id") || state.lastProjectId || "", panel: "images", aux: page === "materials" ? "materials" : "" };
    }
    return { page, displayPage: page === "knowledge" ? "governance" : page, ...(page === "materials" ? {projectId:params.get("project_id") || state.lastProjectId || ""} : {}) };
  }
  if (path === "/projects/new") return { page: "projects", create: true };
  const match = path.match(/^\/projects\/([^/]+)\/(overview|brief|materials|plan|script|shot-images|review|assistant)$/);
  if (!match) return { page: "projects" };
  const projectId = decodeURIComponent(match[1]);
  const originalPage = match[2];
  const creative = ["plan", "script", "shot-images"].includes(originalPage);
  const versionView = originalPage === "script" && params.get("view") === "versions";
  const page = versionView ? "versions" : creative ? "script" : originalPage;
  const requestedPanel = params.get("panel");
  const panel = originalPage === "plan" ? "structure" : ["shot-images", "materials"].includes(originalPage) ? "images"
    : ["structure", "script", "images"].includes(requestedPanel) ? requestedPanel : "script";
  return { projectId, page, navPage: creative ? "script" : originalPage, displayPage: page,
    panel, aux: originalPage === "materials" || params.get("aux") === "materials" ? "materials" : "",
    explicitPanel: Boolean(requestedPanel || originalPage !== "script"),
  };
}

function formalSurface() {
  const route = formalRoute();
  if (route.page !== "script") return route.page;
  return route.aux === "materials" ? "materials" : route.panel === "structure" ? "plan" : route.panel === "images" ? "shot-images" : "script";
}


function creativeState() {
  if (!state.formalCreative) state.formalCreative = {
    projectId: "", versionId: "", shotId: "", selectedShotIds: new Set(), filter: "", editMode: "overview", page: 1,
    batchMode: false, imageMode: "current", imageNavigationScope: null, assistantScope: "current", scroll: new Map(), loaded: false, loadPromise: null,
    navigationRequest: 0, planKey: "", imageKey: "", materialsKey: "", overview: null, overviewError: "",
    imageChanges: new Set(), imageBaseline: new Map(), analysisSelected: new Map(), proposalSelection: new Set(),
    legacyFocusKey: "", locationUnresolved: false, returnPanel: "script", continueImagesAfterSave: false,
  };
  return state.formalCreative;
}

function creativeCurrentShot() {
  const current = creativeState();
  return (state.formalScript?.shots || []).find(shot => shot.shot_id === current.shotId) || null;
}

function creativeViewKey(projectId = creativeState().projectId) {
  return `${state.agentWorkspaceId}:${projectId}`;
}

function captureCreativeInputs() {
  const current = creativeState();
  if (!current.projectId) return;
  const route = formalRoute();
  if (route.page === "script" && route.projectId === current.projectId) {
    current.scroll.set(formalSurface(), window.scrollY || 0);
  }
  state.formalCreativeViews.set(creativeViewKey(), {
    versionId: current.versionId, shotId: current.shotId, selectedShotIds: [...current.selectedShotIds],
    filter: current.filter, editMode: current.editMode, page: current.page, assistantScope: current.assistantScope, imageMode: current.imageMode, imageDetail: Boolean(current.imageDetail), imageNavigationScope: current.imageNavigationScope,
    instruction: elements.formalScriptInstruction.value, reason: elements.formalScriptSaveReason.value,
    scroll: [...current.scroll],
  });
  const {instruction, reason, ...view} = state.formalCreativeViews.get(creativeViewKey());
  try { sessionStorage.setItem(`mplus.creative.view.${creativeViewKey()}`, JSON.stringify(view)); } catch (_) { /* In-memory inputs remain available. */ }
}

function restoreCreativeInputs(model) {
  const current = creativeState();
  let saved = state.formalCreativeViews.get(creativeViewKey());
  if (!saved) { try { saved = JSON.parse(sessionStorage.getItem(`mplus.creative.view.${creativeViewKey()}`) || "null"); } catch (_) { return; } }
  if (!saved) return;
  elements.formalScriptInstruction.value = saved.instruction || "";
  elements.formalScriptSaveReason.value = saved.reason || "";
  if (saved.versionId !== String(model?.base_version_id || "")) return;
  current.shotId = saved.shotId || "";
  current.selectedShotIds = new Set(saved.selectedShotIds || []);
  current.filter = saved.filter || "";
  current.editMode = saved.editMode || "overview";
  current.page = saved.page || 1;
  current.assistantScope = saved.assistantScope || "current";
  current.imageMode = saved.imageMode || "current";
  current.imageDetail = Boolean(saved.imageDetail);
  current.imageNavigationScope = Array.isArray(saved.imageNavigationScope) ? saved.imageNavigationScope : null;
  current.scroll = new Map(saved.scroll || []);
  elements.creativeShotSearch.value = current.filter;
}

function setCreativeLocationNotice(message = "") {
  if (!elements.creativeLocationNotice) return;
  elements.creativeLocationNotice.textContent = message;
  elements.creativeLocationNotice.hidden = !message;
}

function syncCreativeContext(model) {
  const current = creativeState();
  const shots = model?.shots || [];
  const identities = new Set(shots.map(shot => shot.shot_id).filter(Boolean));
  const nextVersion = String(model?.base_version_id || "");
  const changedVersion = Boolean(current.versionId && nextVersion && current.versionId !== nextVersion);
  if (changedVersion) {
    current.planKey = "";
    current.imageKey = "";
    current.materialsKey = "";
    current.imageNavigationScope = null;
    current.imageChanges.clear();
    current.imageBaseline.clear();
    current.proposalSelection.clear();
    const disappeared = current.shotId && !identities.has(current.shotId);
    current.selectedShotIds = new Set([...current.selectedShotIds].filter(id => identities.has(id)));
    if (disappeared) {
      current.shotId = "";
      current.locationUnresolved = true;
      setCreativeLocationNotice("新版本中的镜头关系已变化，原镜头无法直接定位。请从列表选择当前镜头。");
    }
  }
  current.versionId = nextVersion;
  if (!current.shotId && !changedVersion && !current.locationUnresolved) current.shotId = shots.find(shot => shot.shot_id)?.shot_id || "";
  if (current.shotId && !identities.has(current.shotId)) current.shotId = "";
  current.selectedShotIds = new Set([...current.selectedShotIds].filter(id => identities.has(id)));
  renderCreativeOutline();
  syncCreativeShotVisibility();
  syncCreativeNextAction();
  syncCreativeInitialStage();
}

function creativeShotVisual(shot) {
  const pending = state.formalScriptDirtyCells.get(`${shot.shot_id}:visual`);
  return pending?.value ?? shot.visual ?? "";
}

function creativeFilteredShots() {
  const filter = creativeState().filter.trim().toLocaleLowerCase();
  return (state.formalScript?.shots || []).filter(shot => !filter || `${shot.shot_no} ${shot.structure || ""} ${creativeShotVisual(shot)}`.toLocaleLowerCase().includes(filter));
}

function renderCreativeOutline() {
  if (!elements.creativeShotList) return;
  const current = creativeState();
  const shots = state.formalScript?.shots || [];
  const first = state.formalScript?.state === 'empty' ? firstCreativeView() : null;
  if (first?.data?.blocks?.length) {
    elements.creativeShotCount.textContent = `${first.data.blocks.length} 段初稿`;
    elements.creativeShotList.innerHTML = first.data.blocks.map(block => `<li><a class="text-button" href="${escapeHtml(formalPath('script', first.projectId, {panel: 'script', block_id: block.block_id}))}" data-formal-target>第 ${escapeHtml(block.shot_no)} 段 · ${escapeHtml(block.fields['段落'] || block.fields['画面描述'])}</a></li>`).join('');
    elements.creativeSelectionCount.textContent = '初稿按段落编辑与配图';
    const toggle = elements.creativeWorkspace.querySelector('[data-creative-batch-toggle]'); if (toggle) toggle.hidden = true;
    elements.creativeMaterialsContext.textContent = '为已有初稿逐段选图；每张图片都可指定对应的脚本文本。';
    const back = document.querySelector('[data-creative-close-materials]'); if (back) back.textContent = '返回初稿';
    return;
  }
  const toggle = elements.creativeWorkspace.querySelector('[data-creative-batch-toggle]'); if (toggle) toggle.hidden = false;
  const back = document.querySelector('[data-creative-close-materials]'); if (back) back.textContent = creativeCurrentShot() ? '返回原镜头' : '返回创作';
  const loading = state.formalScriptBusy && !state.formalScript;
  const visible = creativeFilteredShots();
  const visibleIds = new Set(visible.map(shot => shot.shot_id));
  const outside = [...current.selectedShotIds].filter(id => !visibleIds.has(id)).length;
  elements.creativeWorkspace.dataset.batchMode = String(current.batchMode);
  const batchToggle = elements.creativeWorkspace.querySelector("[data-creative-batch-toggle]");
  batchToggle?.setAttribute("aria-pressed", String(current.batchMode));
  if (batchToggle) batchToggle.textContent = current.batchMode ? "结束批量" : "批量选择";
  elements.creativeShotCount.textContent = loading ? "读取中" : `${shots.length} 镜`;
  elements.creativeSelectionCount.textContent = `批量选择 ${current.selectedShotIds.size} 镜${outside ? ` · 筛选外 ${outside} 镜` : ""}`;
  elements.creativeShotList.innerHTML = visible.length ? visible.map(shot => {
    const id = String(shot.shot_id || "");
    const imageShot = state.formalImageWorkspace?.shots?.find(item => item.shot_id === id);
    const imageStatus = creativeImageStatus(imageShot);
    return `<li class="creative-shot-item${current.shotId === id ? " is-current" : ""}"><label class="creative-shot-select"><input type="checkbox" data-creative-select-shot="${escapeHtml(id)}" aria-label="选择第 ${escapeHtml(shot.shot_no)} 镜"${current.selectedShotIds.has(id) ? " checked" : ""}${id ? "" : " disabled"}></label><button type="button" data-creative-shot="${escapeHtml(id)}" aria-current="${current.shotId === id}"${id ? "" : " disabled"}><span><b>${escapeHtml(shot.shot_no)}</b><small>${escapeHtml(shot.structure || "镜头")}${shot.assistant_locked ? " · 已保护" : ""}</small></span><strong>${escapeHtml(creativeShotVisual(shot))}</strong><span class="creative-shot-progress">${escapeHtml(imageStatus)}</span></button></li>`;
  }).join("") : `<li class="creative-outline-empty">${loading ? "正在读取当前镜头…" : shots.length ? "没有匹配镜头，调整筛选即可。" : "生成后，镜头会显示在这里。"}</li>`;
  syncCreativeScopes();
  syncCreativeOutlinePosition();
}

function syncCreativeOutlinePosition() {
  const list = elements.creativeShotList;
  const selected = list?.querySelector(".creative-shot-item.is-current");
  if (!selected?.getBoundingClientRect || list.scrollWidth <= list.clientWidth) return;
  const bounds = list.getBoundingClientRect(), item = selected.getBoundingClientRect();
  if (item.left < bounds.left) list.scrollLeft -= bounds.left - item.left;
  else if (item.right > bounds.right) list.scrollLeft += item.right - bounds.right;
}

function syncCreativeScopes() {
  const current = creativeState();
  const shot = creativeCurrentShot();
  const label = mode => mode === "all" ? `整份当前稿 · ${state.formalScript?.shots?.length || 0} 镜`
    : mode === "selected" ? `明确选择的 ${current.selectedShotIds.size} 镜` : shot ? `第 ${shot.shot_no} 镜` : "请先选择一个镜头";
  if (elements.creativeAssistantScope) elements.creativeAssistantScope.value = current.assistantScope;
  const assistantIds = new Set(creativeAllowedShotIds(current.assistantScope, false));
  const protectedCount = (state.formalScript?.shots || []).filter(item => item.assistant_locked && assistantIds.has(item.shot_id)).length;
  if (elements.creativeAssistantScopeCopy) elements.creativeAssistantScopeCopy.textContent = `${label(current.assistantScope)}${protectedCount ? ` · 已排除 ${protectedCount} 镜保护内容` : ""}；助手可阅读整稿，实际修改限定此范围。`;
  if (elements.creativeImageScope) elements.creativeImageScope.value = current.imageMode;
  if (elements.creativeImageScopeCopy) elements.creativeImageScopeCopy.textContent = `${current.imageNavigationScope ? `保留原范围 ${current.imageNavigationScope.length} 镜；当前仅切换查看位置` : label(current.imageMode)} · 仅切换浏览范围，保存包含全部明确修改`;
  document.querySelectorAll('[data-preview-image-confirmation]').forEach(button => {
    const total = state.formalImageWorkspace?.shots?.length;
    const selected = current.selectedShotIds.size;
    button.textContent = button.dataset.previewImageConfirmation === 'all'
      ? `确认全部配图${total ? ` · ${total} 镜` : ''}` : `确认所选配图 · ${selected} 镜`;
    button.disabled = !total || button.dataset.previewImageConfirmation === 'selected' && !selected;
  });
  const pickingForShot = new URLSearchParams(window.location.search).get("pick") === "library" || new URLSearchParams(window.location.search).has("focus_shot");
  if (elements.creativeMaterialsContext) elements.creativeMaterialsContext.textContent = shot && pickingForShot
    ? `从第 ${shot.shot_no} 镜打开 · ${creativeShotVisual(shot)}。加入素材后，仍需确认本镜配图。`
    : "浏览项目素材。加入素材与保存镜头配图分别确认。";
}

function creativeAllowedShotIds(mode = creativeState().imageMode, forImages = true) {
  const current = creativeState();
  if (forImages && current.imageNavigationScope) return current.imageNavigationScope.filter(id => (state.formalScript?.shots || []).some(shot => shot.shot_id === id));
  const existing = new Set((state.formalScript?.shots || state.formalImageWorkspace?.shots || []).map(shot => shot.shot_id).filter(Boolean));
  if (mode === "all") return [...existing];
  if (mode === "selected") return [...current.selectedShotIds].filter(id => existing.has(id));
  return current.shotId && existing.has(current.shotId) ? [current.shotId] : [];
}

function syncCreativeShotVisibility() {
  const current = creativeState();
  if (!elements.creativeWorkspace) return;
  elements.creativeWorkspace.dataset.editMode = current.editMode;
  elements.formalScriptTable.querySelectorAll("[data-optional-field]").forEach(detail => {
    detail.open = current.editMode === "table" || Boolean(creativeOptionalFields.get(detail.dataset.optionalField));
  });
  elements.formalScriptTable.querySelectorAll("[data-script-shot-id]").forEach(row => {
    row.hidden = current.editMode !== "table" && row.dataset.scriptShotId !== current.shotId;
    if (row.dataset.layoutMode !== current.editMode) {
      const cells = [...row.children];
      cells.forEach((cell, index) => { cell.dataset.tableColumn ??= String(index); });
      const readingOrder = [0, 4, 5, 6, 7, 8, 2, 3, 1, 9];
      cells.sort((left, right) => current.editMode === "table"
        ? Number(left.dataset.tableColumn) - Number(right.dataset.tableColumn)
        : readingOrder.indexOf(Number(left.dataset.tableColumn)) - readingOrder.indexOf(Number(right.dataset.tableColumn)));
      row.append(...cells);
      row.dataset.layoutMode = current.editMode;
    }
  });
  const allowed = new Set(creativeAllowedShotIds());
  elements.formalImageShotGrid.querySelectorAll("[data-image-shot]").forEach(card => {
    const shot = state.formalImageWorkspace?.shots?.[Number(card.dataset.imageShot)];
    card.hidden = !shot?.shot_id || (current.imageNavigationScope ? shot.shot_id !== current.shotId : !allowed.has(shot.shot_id));
    const fieldset = card.querySelector("[data-image-edit-scope]");
    if (fieldset) fieldset.disabled = !allowed.has(shot?.shot_id);
    const note = card.querySelector("[data-image-scope-notice]");
    if (note) note.hidden = allowed.has(shot?.shot_id);
  });
  const imageIndex = state.formalImageWorkspace?.shots?.findIndex(shot => shot.shot_id === current.shotId) ?? -1;
  const jump = document.querySelector("#formal-image-jump");
  if (jump) jump.value = imageIndex >= 0 ? String(imageIndex) : "";
  elements.creativeWorkspace.querySelectorAll("[data-creative-edit-mode]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.creativeEditMode === current.editMode)));
  renderCreativeCurrentImage();
  renderCreativePendingIssues();
  syncCreativeScopes();
  renderWorkbenchCards();
}

function selectCreativeShot(shotId, { updateUrl = true, field = "", focus = false, preserveImageScope = false } = {}) {
  const current = creativeState();
  const shot = (state.formalScript?.shots || []).find(item => item.shot_id === shotId);
  if (!shot) {
    current.shotId = "";
    current.locationUnresolved = true;
    renderCreativeOutline();
    syncCreativeShotVisibility();
    setCreativeLocationNotice("原镜头已变化或无法定位，请从当前镜头列表重新选择。");
    return false;
  }
  if (!preserveImageScope) current.imageNavigationScope = null;
  current.shotId = shotId;
  current.locationUnresolved = false;
  if (current.imageMode === "current" && !preserveImageScope) invalidateFormalImageVersionProposal();
  setCreativeLocationNotice();
  renderCreativeOutline();
  syncCreativeShotVisibility();
  if (updateUrl && formalRoute().page === "script") {
    const url = new URL(window.location.href);
    url.searchParams.set("shot_id", shotId);
    url.searchParams.delete("focus_shot");
    window.history.replaceState({}, "", `${url.pathname}${url.search}`);
  }
  if (focus) {
    const selector = `[data-script-shot-key="${CSS.escape(shotId)}"]${field ? `[data-script-field="${CSS.escape(field)}"]` : ""}`;
    const control = elements.formalScriptTable.querySelector(selector);
    const optional = control?.closest("details");
    if (optional) optional.open = true;
    control?.focus({ preventScroll: true });
    control?.scrollIntoView({ block: "nearest" });
  }
  return true;
}

function creativeReferenceMarkup(shotId, open = false) {
  const refs = (state.formalImageWorkspace?.creative_references || []).filter(item => item.shot_id === shotId);
  if (!refs.length) return '';
  return `<details class="creative-reference-collection"${open ? ' open' : ''}><summary>初稿参考图 · ${refs.length} 张</summary><p>保留初稿与本镜的对应关系；正式配图仍需选用和核对。</p><div class="creative-reference-grid">${refs.map(ref => `<figure><div data-formal-creative-reference="${escapeHtml(ref.reference_id || ref.task_id)}"></div><figcaption>${ref.primary ? '主参考 · ' : ''}${escapeHtml(ref.origin_label || '初稿参考图')}</figcaption></figure>`).join('')}</div></details>`;
}
function loadCreativeReferencePreviews(details) {
  if (!details?.open) return;
  const workspace = state.formalImageWorkspace;
  for (const host of details.querySelectorAll('[data-formal-creative-reference]')) {
    if (host.dataset.loaded) continue;
    const ref = workspace?.creative_references?.find(item => (item.reference_id || item.task_id) === host.dataset.formalCreativeReference);
    if (!ref?.preview_url) { host.textContent = '参考图暂未提供预览'; continue; }
    host.dataset.loaded = 'loading'; host.textContent = '正在读取参考图…';
    void requestFormalPreview(ref.preview_url, {owner: `creative-references:${formalRoute().projectId}`, priority: 1}).then(url => {
      if (!host.isConnected || workspace !== state.formalImageWorkspace) return;
      const image = document.createElement('img'); image.src = url; image.alt = ref.origin_label || '初稿参考图'; host.replaceChildren(image); host.dataset.loaded = 'ready';
    }).catch(() => { if (host.isConnected) { delete host.dataset.loaded; host.textContent = '参考图读取失败，收起后展开可重试'; } });
  }
}
document.addEventListener('toggle', event => {
  if (event.target.matches?.('.creative-reference-collection')) loadCreativeReferencePreviews(event.target);
}, true);

function renderCreativeCurrentImage() {
  if (!elements.creativeCurrentImage) return;
  const shot = creativeCurrentShot();
  if (!shot) { elements.creativeCurrentImage.innerHTML = ""; return; }
  const imageShot = (state.formalImageWorkspace?.shots || []).find(item => item.shot_id === shot.shot_id);
  const {candidate, url, label} = formalImageDisplay(imageShot);
  const content = candidate?.actual_caption || "图片内容尚未单独识别";
  const usage = candidate?.template_usage || imageShot?.shot_function || "未提供模板用途";
  if (!elements.creativeCurrentImage.querySelector("[data-current-image-info]")) {
    elements.creativeCurrentImage.innerHTML = `<div class="creative-image-thumbnail"></div><div><div data-current-image-info></div><div class="ui00-inline-actions"><button type="button" class="secondary-button compact" data-creative-change-image>换图</button><button type="button" class="text-button" data-creative-open-tool="assistant">智能修改</button><button type="button" class="text-button" data-creative-create-image>生成新图</button><button type="button" class="text-button" data-creative-lock-current>保护本镜</button></div></div>`;
  }
  const thumbnail = elements.creativeCurrentImage.querySelector(".creative-image-thumbnail");
  const pixelMarkup = url ? `<img src="${escapeHtml(url)}" alt="第 ${escapeHtml(shot.shot_no)} 镜${escapeHtml(label)}">` : `<span>${state.formalScript?.can_save_version ? "当前稿待保存" : "本版本没有参考图或尚未读取"}</span>`;
  if (thumbnail.dataset.rendered !== pixelMarkup) { thumbnail.innerHTML = pixelMarkup; thumbnail.dataset.rendered = pixelMarkup; }
  const info = elements.creativeCurrentImage.querySelector("[data-current-image-info]");
  const detailsOpen = Boolean(info.querySelector("details:not(.creative-reference-collection)")?.open);
  const refsOpen = Boolean(info.querySelector('.creative-reference-collection')?.open);
  const infoMarkup = `<span>${escapeHtml(label)}</span><strong>${escapeHtml(content)}</strong><details${detailsOpen ? " open" : ""}><summary>图片、模板与镜头意图</summary><p>图片实际内容：${escapeHtml(content)}</p><p>模板用途：${escapeHtml(usage)}</p><p>当前镜头意图：${escapeHtml(imageShot?.visual || shot.visual || "待填写")}</p></details>${creativeReferenceMarkup(shot.shot_id, refsOpen)}`;
  if (info.dataset.rendered !== infoMarkup) { info.innerHTML = infoMarkup; info.dataset.rendered = infoMarkup; }
  loadCreativeReferencePreviews(info.querySelector('.creative-reference-collection'));
  elements.creativeCurrentImage.querySelector("[data-creative-lock-current]").textContent = shot.assistant_locked ? "解除保护" : "保护本镜";
}

function syncCreativeVisibility(route = formalRoute()) {
  if (route.page !== "script" || !elements.creativeWorkspace) return;
  const current = creativeState();
  if (creativeImagePicker && (!new URLSearchParams(window.location.search).has("picker") || creativeImagePicker.projectId !== route.projectId)) closeCreativeImagePicker({ updateUrl: false });
  const visible = route.aux === "materials" ? "page-materials" : route.panel === "structure" ? "page-plan" : route.panel === "images" ? "page-shot-images" : "creative-script-panel";
  for (const id of ["creative-script-panel", "page-plan", "page-shot-images", "page-materials"]) {
    const panel = document.getElementById(id);
    if (panel) { panel.hidden = id !== visible; panel.classList.toggle("active", id === visible); }
  }
  elements.creativeWorkspace.querySelectorAll("[data-creative-panel]").forEach(button => {
    const active = button.dataset.creativePanel === route.panel;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  elements.creativeWorkspace.dataset.panel = route.panel;
  elements.creativeWorkspace.dataset.aux = route.aux || "";
  const draft = Boolean(state.formalScript?.can_save_version || formalScriptHasPendingWrites());
  elements.creativeImageDraftGate.hidden = true;
  if (state.formalImageWorkspace?.shots?.length) elements.formalShotImageWorkbench.hidden = false;
  if (route.aux !== "materials") current.returnPanel = route.panel;
  syncCreativeShotVisibility();
  syncCreativeNextAction();
  syncCreativeInitialStage(route);
}

function syncCreativeNextAction() {
  if (!elements.creativeNextAction) return;
  const current = creativeState(), draft = state.formalScript, queue = creativeProgressQueue();
  const shots = state.formalImageWorkspace?.shots || [];
  const changed = shots.filter(shot => shot.explicitly_changed);
  const incomplete = shots.find(shot => (!shot.confirmed && !shot.explicitly_changed)
    || formalImageDecisionIssue(shot, { requirePreview: Boolean(!shot.confirmed || shot.explicitly_changed) }));
  const action = current.overview?.next_action;
  let label = draft?.version_label || "从需求与结构开始", copy = action?.reason || "按当前项目状态继续创作。";
  let command = "", button = action?.label || "继续", href = action?.href ? formalCanonicalPath(action.href) : "";
  if (formalScriptHasPendingWrites()) {
    label = "文字修改尚未保存";
    copy = state.formalScriptSaveError ? elements.formalScriptSaveState.textContent : "正在保存当前修改，完成后可核对并保存新版本。";
    command = ""; href = "";
  } else if (current.overviewLoading) {
    label = "正在核对项目状态";
    copy = "当前稿可继续查看和编辑，下一步将在核对完成后显示。";
    href = "";
  } else if (shots.length && incomplete) {
    label = `配图进行中 · 全稿 ${shots.length} 镜`;
    copy = `第 ${incomplete.shot_no} 镜：${formalImageDecisionIssue(incomplete, {requirePreview:false}) || "待核对图片"}。当前修改可保存进度，配齐后统一保存新版本。`;
    command = "pending"; button = `继续第 ${incomplete.shot_no} 镜`; href = formalPath("script", current.projectId, { panel: "images", shot_id: incomplete.shot_id });
  } else if (draft?.can_save_version) {
    label = "当前稿已保存进度，待形成新版本";
    copy = "文字与配图将一并保存为一个版本。";
    command = "script"; button = "核对改动并保存版本"; href = formalPath("script", current.projectId, { action: "save" });
  } else if (changed.length) {
    label = "配图已选用，待保存版本"; copy = `本次 ${changed.length} 镜修改，确认版本影响后进入审核。`;
    command = "images"; button = "预览并保存新版本"; href = formalPath("script", current.projectId, { panel: "images" });
  } else if (current.overviewError || current.overview?.draft?.state === "unknown") {
    label = "当前状态需要重新读取"; copy = current.overviewError || "暂时无法核实草稿状态，请重新读取后继续。"; href = "";
  } else if (current.overview?.saved_delivery && action?.href
    && current.overview?.draft?.base_version_id === draft?.base_version_id) {
    label = `${current.overview.saved_delivery.version_label || draft?.version_label || "本版"} · 整片已确认`;
    copy = action.reason || "可查看审核记录和交付文件。";
    button = action.label; href = formalCanonicalPath(action.href);
  } else if (shots.length && shots.every(shot => shot.confirmed)) {
    label = "配图版本已保存"; copy = "进入人工审核，核对镜头并完成整片确认。";
    button = "进入审核"; href = formalPath("review", current.projectId);
    if (current.overview?.draft?.base_version_id === draft?.base_version_id && action?.href === href) {
      button = action.label || button;
      copy = action.reason || copy;
    }
  }
  const savedDelivery = current.overview?.saved_delivery;
  const savedVersionId = current.overview?.draft?.base_version_id;
  const savedLink = document.querySelector("#creative-saved-delivery");
  savedLink.hidden = !savedDelivery || savedVersionId !== draft?.base_version_id
    || !(draft?.can_save_version || formalScriptHasPendingWrites() || changed.length || incomplete);
  savedLink.textContent = savedDelivery ? `${savedDelivery.version_label} · 已确认，查看本版交付` : "";
  savedLink.href = savedDelivery && savedVersionId
    ? formalPath("review", current.projectId, { version_id: savedVersionId }) : "/projects";
  elements.creativeNextLabel.textContent = label;
  elements.creativeNextCopy.textContent = copy;
  elements.creativeNextAction.hidden = !href;
  elements.creativeNextAction.href = href || "/projects";
  elements.creativeNextAction.dataset.action = command;
  elements.creativeNextAction.setAttribute("aria-disabled", String(command === "images" && state.formalImageVersionBusy));
  elements.creativeNextAction.textContent = button;
  syncCreativeProgressStatus();
  syncCreativePrimaryAction();
}

function closeCreativeMenus({ returnFocus = false } = {}) {
  document.querySelectorAll(".creative-more[open]").forEach(menu => {
    menu.open = false;
    if (returnFocus) menu.querySelector("summary")?.focus({ preventScroll: true });
  });
}

document.addEventListener("click", event => {
  document.querySelectorAll(".creative-more[open]").forEach(menu => {
    if (!menu.contains(event.target) || event.target.closest("nav a, nav button")) menu.open = false;
  });
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && document.querySelector(".creative-more[open]")) {
    closeCreativeMenus({ returnFocus: true });
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

function syncCreativePrimaryAction() {
  if (!elements.creativeWorkspace) return;
  const route = formalRoute();
  let primary = elements.creativeNextAction;
  if (route.panel === "structure") primary = state.formalPlanConflictProposal ? elements.formalPlanConflictExecute
    : state.formalPlanPreview ? elements.formalPlanConfirm : elements.formalPlanCreate;
  if (state.formalImageVersionProposal && route.panel === "images") primary = elements.formalImageVersionProposal.querySelector("[data-confirm-image-version]");
  if (!elements.formalScriptSaveProposal.hidden) primary = elements.formalScriptSaveProposal.querySelector("[data-confirm-script-revision]") || primary;
  elements.creativeWorkspace.querySelectorAll(".primary-button").forEach(button => button.classList.toggle("creative-main-action", button === primary));
}

async function refreshCreativeOverview(projectId) {
  const current = creativeState();
  const auth = state.agentAuth, workspaceId = state.agentWorkspaceId;
  const request = current.overviewRequest = (current.overviewRequest || 0) + 1;
  const active = () => creativeState() === current && current.projectId === projectId
    && current.overviewRequest === request && state.agentAuth === auth && state.agentWorkspaceId === workspaceId;
  current.overviewLoading = true;
  syncCreativeNextAction();
  try {
    const overview = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/overview`);
    if (!active()) return;
    current.overview = overview;
    current.overviewError = "";
  } catch (error) {
    if (!active()) return;
    current.overviewError = formalError(error);
  } finally {
    if (active()) { current.overviewLoading = false; syncCreativeNextAction(); }
  }
}

function firstCreativeView(projectId = formalRoute().projectId) {
  const key = JSON.stringify([state.agentAuth, state.agentWorkspaceId, projectId]);
  if (!state.formalFirstDraftViews.has(key)) state.formalFirstDraftViews.set(key, {
    projectId, auth: state.agentAuth, workspaceId: state.agentWorkspaceId,
    data: null, dirty: new Map(), timer: null, saveTimer: null, saving: null,
    error: "", conflict: false, busy: false, renderKey: "",
    retry: null, startRetry: null, selected: new Set(), activeBlockId: "", editMode: "overview", page: 1, search: "",
  });
  const view = state.formalFirstDraftViews.get(key);
  if (!view.restoredWorkbenchView) {
    view.restoredWorkbenchView = true;
    try { const saved = JSON.parse(sessionStorage.getItem(`mplus.first.view.${state.agentWorkspaceId}:${projectId}`) || "null"); if (saved) { Object.assign(view, saved, {selected: new Set(saved.selected || [])}); } } catch (_) {}
  }
  return view;
}

function firstCreativePath(view, action = "") {
  return `/api/formal/projects/${encodeURIComponent(view.projectId)}/creative-draft${action ? `/${action}` : ""}?workspace_id=${encodeURIComponent(view.workspaceId)}`;
}

function currentInitialDraft(projectId = formalRoute().projectId) {
  if (!projectId || state.formalScriptProjectId !== projectId || state.formalScript?.state !== 'empty') return null;
  const view = firstCreativeView(projectId);
  return view.data?.blocks?.length ? view : null;
}

function useCurrentCreativePlan(projectId = formalRoute().projectId) {
  return Boolean(currentInitialDraft(projectId)) || new URLSearchParams(window.location.search).get('creative') === '1';
}

function syncCreativeInitialStage(route = formalRoute()) {
  elements.creativeWorkspace.classList.toggle('creative-initial-structure',
    route.page === 'script' && route.panel === 'structure' && Boolean(currentInitialDraft(route.projectId)));
}

function firstCreativeVisible(view) {
  return view.auth === state.agentAuth && view.workspaceId === state.agentWorkspaceId
    && formalRoute().page === "script" && formalRoute().projectId === view.projectId
    && !document.querySelector("#creative-first-draft").hidden;
}

function acceptFirstCreative(view, data) {
  if (!data || data.project_id !== view.projectId || Number(data.revision) < Number(view.data?.revision || 0)) return;
  if (data.response_kind === "delta") {
    if (!view.data || data.brief_marker !== view.data.brief_marker || data.revision !== view.data.revision) return;
    data = { ...view.data, ...data };
  }
  const previousTaskId = view.data?.task?.task_id;
  if (data.stream_reset === false && previousTaskId === data.task?.task_id) {
    const merged = new Map((view.data?.stream_blocks || []).map(block => [block.block_id, block]));
    for (const block of data.stream_blocks || []) merged.set(block.block_id, block);
    data.stream_blocks = [...merged.values()];
    if (data.task?.status === "waiting_user" && !data.proposal) data.proposal = view.data?.proposal;
    if (!data.notice) data.notice = view.data?.notice || "";
  }
  view.data = data;
  if (data.task?.is_running || !["received", "executing"].includes(data.task?.status)) view.unlockedSince = null;
  else if (!view.unlockedSince || previousTaskId !== data.task?.task_id) view.unlockedSince = Date.now();
  if (state.formalScript?.state === "empty" && state.formalScriptProjectId === view.projectId)
    state.formalScript.project_version = data.project_version;
  if (creativeState().projectId === view.projectId) creativeState().planKey = "";
}

function firstCreativePending(view) {
  return Boolean(view.data?.task?.is_running || (["received", "executing"].includes(view.data?.task?.status)
    && view.unlockedSince && Date.now() - view.unlockedSince < 10000));
}

function firstCreativeSeconds(value) {
  const match = String(value ?? "").trim().match(/^(?:建议\s*)?([1-9]\d*)\s*(?:秒|s)?$/);
  return match ? Number(match[1]) : null;
}

function firstCreativeProposalStale(view) {
  const proposal = view.data?.proposal;
  return Boolean(proposal && (proposal.stale || proposal.revision !== view.data.revision));
}

function renderFirstProposalDuration(view) {
  const host = document.querySelector('#creative-first-proposal-duration');
  const proposal = view.data?.proposal;
  if (!host || !proposal) return;
  if (firstCreativeProposalStale(view) || view.dirty.size) {
    host.textContent = '需求或初稿已变化，请重新预览建议后核对预计时长。';
    return;
  }
  const groups = proposal.groups || [];
  const selected = groups.filter(group => view.retainedBlocks.has(group.block_id));
  const status = proposal.duration_status;
  const target = status && 'target_seconds' in status ? status.target_seconds : view.data.duration_status?.target_seconds;
  let actual, pending;
  if (selected.length && selected.length === groups.length && status && Number.isFinite(status.actual_seconds)) {
    actual = status.actual_seconds;
    pending = Number(status.pending_block_count || 0);
  } else {
    const durations = new Map((view.data.blocks || []).map(block => [block.block_id, firstCreativeSeconds(block.fields['时长'])]));
    for (const group of selected) for (const change of group.changes || []) {
      if (change.field === '时长' && durations.has(group.block_id)) durations.set(group.block_id, firstCreativeSeconds(change.after));
    }
    pending = [...durations.values()].filter(value => value === null).length;
    actual = [...durations.values()].reduce((sum, value) => sum + (value || 0), 0);
  }
  const knownTarget = Number.isFinite(target), difference = knownTarget ? actual - target : null;
  const summary = `${selected.length ? `采用所选 ${selected.length} 组后预计` : '未选择建议，当前'} ${actual} 秒${knownTarget ? ` / 目标 ${target} 秒` : ' · 尚无已确认目标时长'}${pending ? ` · ${pending} 段时长待定` : knownTarget ? difference === 0 ? ' · 预计与目标一致' : ` · ${difference > 0 ? '仍超出' : '仍差'} ${Math.abs(difference)} 秒` : ''}`;
  const needsAdjustment = knownTarget && (pending || difference !== 0);
  const note = pending ? '仍有待定时长，补齐后再核对目标。' : needsAdjustment ? '尚未达到目标，可继续调整；采用只保存所选修改。' : '这是采用前的预览，当前稿尚未改变。';
  host.innerHTML = `<strong>${escapeHtml(summary)}</strong><p>${escapeHtml(note)}</p>${needsAdjustment ? `<button type="button" class="text-button" data-first-proposal-duration-adjust${view.busy || firstCreativePending(view) ? ' disabled' : ''}>继续按目标调整</button>` : ''}`;
}

function renderFirstCreativeScope(view) {
  const blocks = view.data?.blocks || [];
  const numbers = ids => blocks.filter(block => ids.has(block.block_id)).map(block => block.shot_no).join("、") || "无";
  const scope = document.querySelector("#creative-first-revision-scope").value;
  const allowed = scope === "all" ? "整份初稿" : scope === "current" ? `当前第 ${blocks.find(block => block.block_id === view.activeBlockId)?.shot_no || "—"} 段` : `已选 ${numbers(view.selected)} 段`;
  const fields = [...new Set((view.data?.proposal?.groups || []).flatMap(group => group.changes.map(change => change.field)))].join("、") || "按修改要求预览";
  document.querySelector("#creative-first-revision-scope-copy").textContent = `允许修改：${allowed}；保留 ${numbers(new Set(view.data?.protected_block_ids || []))} 段；修改字段：${fields}`;
  const durations = blocks.map(block => firstCreativeSeconds(view.dirty.get(`${block.block_id}:时长`)?.value ?? block.fields["时长"]));
  const pending = durations.filter(value => value === null).length;
  const actual = durations.reduce((sum, value) => sum + (value || 0), 0);
  const target = view.data?.duration_status?.target_seconds;
  const difference = Number.isFinite(target) ? actual - target : null;
  document.querySelector("#creative-first-summary").textContent = `${blocks.length} 段 · 当前 ${actual} 秒${Number.isFinite(target) ? ` / 目标 ${target} 秒${pending ? '' : difference === 0 ? ' · 时长一致' : ` · ${difference > 0 ? '超出' : '还差'} ${Math.abs(difference)} 秒`}` : ''}${pending ? ` · ${pending} 段时长待定` : ""}`;
  const align = document.querySelector('#creative-first-align-duration');
  align.hidden = !blocks.length || !Number.isFinite(target) || (!pending && difference === 0);
  align.disabled = view.busy || firstCreativePending(view);
  renderFirstProposalDuration(view);
}

function renderFirstCreativeNavigation(view) {
  const blocks = view.data?.blocks || [];
  if (!blocks.some(block => block.block_id === view.activeBlockId)) view.activeBlockId = blocks[0]?.block_id || '';
  const query = (view.search || '').trim().toLocaleLowerCase();
  const shown = blocks.filter(block => !query || Object.values(block.fields).some(value => String(value).toLocaleLowerCase().includes(query)) || String(block.shot_no).includes(query));
  document.querySelector('#creative-first-block-count').textContent = `${blocks.length} 段`;
  document.querySelector('#creative-first-outline').innerHTML = shown.map(block => `<li><button type="button" class="creative-shot-item${block.block_id === view.activeBlockId ? ' is-current' : ''}" data-first-jump="${escapeHtml(block.block_id)}" aria-current="${block.block_id === view.activeBlockId ? 'true' : 'false'}"><span>${escapeHtml(block.shot_no)}</span><strong>${escapeHtml(block.fields['段落'] || block.fields['画面描述'] || '待填写')}</strong><small>${escapeHtml(block.fields['时长'] || '时长待定')}${view.selected.has(block.block_id) ? ' · 已选' : ''}${(view.data.protected_block_ids || []).includes(block.block_id) ? ' · 已保护' : ''}</small></button></li>`).join('') || '<li class="workbench-empty">没有匹配段落</li>';
  const index = blocks.findIndex(block => block.block_id === view.activeBlockId);
  document.querySelector('#creative-first-position').textContent = index >= 0 ? `${index + 1} / ${blocks.length}` : '';
  document.querySelectorAll('[data-first-step]').forEach(button => { button.disabled = !blocks.length || (Number(button.dataset.firstStep) < 0 ? index <= 0 : index >= blocks.length - 1); });
  document.querySelectorAll('[data-first-view]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.firstView === view.editMode)));
  const host = document.querySelector('#creative-first-blocks');
  host.classList.toggle('is-table', view.editMode === 'table');
  host.querySelectorAll('[data-first-row]').forEach(row => { row.hidden = view.editMode !== 'table' && row.dataset.firstRow !== view.activeBlockId; });
  renderWorkbenchCards();
}

function selectFirstCreativeBlock(view, blockId) {
  if (!view.data?.blocks?.some(block => block.block_id === blockId)) return;
  view.activeBlockId = blockId;
  const url = new URL(window.location.href);
  url.searchParams.set('block_id', blockId);
  url.searchParams.set('draft_view', view.editMode);
  window.history.replaceState({}, '', `${url.pathname}${url.search}`);
  renderFirstCreativeNavigation(view);
  if (view.editMode === 'table') [...document.querySelectorAll('[data-first-row]')].find(row => row.dataset.firstRow === blockId)?.scrollIntoView({block: 'nearest'});
}

function renderFirstCreative(view) {
  if (!firstCreativeVisible(view)) return;
  const data = view.data;
  const running = firstCreativePending(view);
  const saved = data?.blocks || [];
  const blocks = saved;
  const confirmedBrief = data?.understanding?.confirmation_status === "confirmed";
  document.querySelector("#creative-first-intake-link").href = formalPath("brief", view.projectId);
  document.querySelector("#creative-first-intake-link").textContent = confirmedBrief ? "返回需求" : "返回需求并确认";
  document.querySelector("#creative-first-title").textContent = saved.length ? "修改脚本" : "开始创作";
  if (!saved.length) document.querySelector('.creative-first-tools').open = true;
  document.querySelector("#creative-first-confirmed-start").hidden = !confirmedBrief;
  document.querySelector("#creative-first-confirmed-start").textContent = saved.length ? "按确认需求重新起稿" : "按确认的需求创作";
  document.querySelector("#creative-first-confirmed-start").disabled = view.busy || running;
  document.querySelector("#creative-first-entry-copy").textContent = saved.length ? "在此修改脚本表达；目标、风格或资料变更请返回需求页。" : confirmedBrief ? "需求已确认，可以开始创作。" : "需求尚未确认，请先返回需求页完成核对。";
  const templatePanel = document.querySelector('#creative-first-template');
  const template = data?.template_preview?.selected;
  templatePanel.hidden = !template;
  if (template) templatePanel.innerHTML = `<strong>${saved.length ? '后续起稿模板' : '本次起稿模板'}：${escapeHtml(formalTemplateLabel(template))}</strong><p>${escapeHtml(data.template_preview.recommendation)}</p><p>${escapeHtml((template.structures || []).map(part => part.name).join(' → '))}</p>${data.creation_basis?.template_ref ? `<p>当前稿冻结模板：${escapeHtml(data.creation_basis.template_ref)}</p>` : ''}<a class="text-button" data-formal-target href="${escapeHtml(formalPath('brief', view.projectId))}?panel=templates">核对原文或更换模板</a>`;
  const status = document.querySelector("#creative-first-status");
  status.textContent = view.saveError || (view.readError ? view.error : "") || (view.saving ? "正在保存你的修改…" : view.dirty.size ? "修改待保存" : saved.length ? "初稿已保存。修改后自动保存。" : confirmedBrief ? "需求已确认，可以开始创作。" : "请先分析并确认需求。");
  status.dataset.needsAttention = String(Boolean(view.saveError || view.readError || view.saving || view.dirty.size || !saved.length));
  const assistantStatus = document.querySelector("#creative-first-assistant-status");
  assistantStatus.textContent = (!view.readError && !view.saveError ? view.error : "") || (view.busy || running ? data?.task_kind === "revision" ? "正在分析本次修改…" : "正在按需求创作…" : data?.proposal ? "修改建议已准备好，请核对下方差异。" : data?.notice || (data?.task?.status === "failed" ? data?.task_kind === "revision" ? "本次修改尚未完成，当前稿已保留。可调整修改要求后重新预览。" : "本次创作尚未完成，已有内容已保留。可按确认需求重新起稿。" : ""));
  assistantStatus.hidden = !assistantStatus.textContent;
  assistantStatus.dataset.needsAttention = String(Boolean(view.error || view.busy || running || data?.task?.status === 'failed'));
  renderFirstCreativeScope(view);
  document.querySelector("#creative-first-conflict").hidden = !view.conflict;
  document.querySelector('[data-first-action="undo"]').disabled = !data?.can_undo || view.busy || Boolean(view.saving);
  document.querySelector('[data-first-action="redo"]').disabled = !data?.can_redo || view.busy || Boolean(view.saving);
  document.querySelector("#creative-first-save").disabled = !view.dirty.size || Boolean(view.saving);
  document.querySelector("#creative-first-assistant").hidden = !saved.length;
  document.querySelector("#creative-first-revision-submit").disabled = view.busy || running;
  const planLink = document.querySelector("#creative-first-plan-link");
  planLink.href = formalPath("script", view.projectId, { panel: "structure", ...(saved.length ? { creative: "1" } : {}) });
  planLink.hidden = !confirmedBrief && !saved.length;
  planLink.textContent = saved.length ? "按初稿生成正式规划" : "查看结构与规划";
  planLink.setAttribute("aria-disabled", String(Boolean(view.dirty.size || view.saving || running)));
  const renderKey = `${data?.revision}:${saved.length}:${view.dirty.size}:${running}:${confirmedBrief}`;
  if (view.renderKey !== renderKey) {
    const focused = document.activeElement;
    const selection = focused?.matches?.("[data-first-field]") ? {
      block: focused.dataset.firstBlock, field: focused.dataset.firstField,
      start: focused.selectionStart, end: focused.selectionEnd,
    } : null;
    document.querySelector("#creative-first-blocks").innerHTML = blocks.length ? blocks.map((block, index) => {
      const editable = saved.length > 0;
      return `<article class="creative-first-block" data-first-row="${escapeHtml(block.block_id)}"><div class="creative-first-block-heading"><span>${String(index + 1).padStart(2, "0")}</span>${editable ? `<div class="creative-first-block-controls"><label><input type="checkbox" data-first-select="${escapeHtml(block.block_id)}" ${view.selected.has(block.block_id) ? "checked" : ""} />选择</label><label><input type="checkbox" data-first-protect="${escapeHtml(block.block_id)}" ${(data.protected_block_ids || []).includes(block.block_id) ? "checked" : ""} />助手保留</label><button class="text-button" type="button" data-first-pick-image="${escapeHtml(block.block_id)}">选图</button><button class="text-button" type="button" data-first-image="${escapeHtml(block.block_id)}">生成新图</button><button class="text-button" type="button" data-first-delete="${escapeHtml(block.block_id)}">删除这段</button></div>` : '<small>正在生成，尚未保存为完整初稿</small>'}</div><p class="creative-first-image-intent">${escapeHtml(block.fields["画面描述"] || "请先填写本段画面描述")}</p>${["段落", "画面描述", "声音", "时长"].map(field => {
        const value = block.fields[field] || "";
        const pending = view.dirty.get(`${block.block_id}:${field}`);
        if (field === "时长") {
          const duration = pending ? pending.value : firstCreativeSeconds(value) ?? "";
          return `<label><span>时长</span><div class="creative-first-duration"><input type="number" min="1" step="1" inputmode="numeric" aria-label="第${block.shot_no}段时长" placeholder="待定" data-first-block="${escapeHtml(block.block_id)}" data-first-field="时长" value="${escapeHtml(duration)}"><span>秒</span><small>留空为待定</small></div></label>`;
        }
        return `<label><span>${escapeHtml(field)}</span><textarea rows="${field === "画面描述" ? 3 : 1}" data-first-block="${escapeHtml(block.block_id)}" data-first-field="${escapeHtml(field)}" ${editable ? "" : "readonly"}>${escapeHtml(pending ? pending.value : value)}</textarea></label>`;
      }).join("")}${(data?.image_references || []).filter(ref => ref.block_id === block.block_id).map(ref => `<figure class="creative-first-reference"><div data-first-reference="${escapeHtml(ref.reference_id || ref.task_id)}"></div><figcaption>${ref.primary ? "主图 · " : ""}${escapeHtml(ref.origin_label)} · 初稿参考，转正式后需核对</figcaption></figure>`).join("")}</article>`;
    }).join("") : `<div class="creative-first-empty"><strong>${running ? "助手正在创作" : confirmedBrief ? "需求已就绪" : "先把需求理清"}</strong><p>${running ? "完整初稿准备好后会显示在这里，可以离开后再回来查看。" : confirmedBrief ? "开始创作后，在这里查看和编辑完整初稿。" : "在需求工作区对话、上传文档或详细录入，核对后再开始创作。"}</p></div>`;
    renderFirstCreativeNavigation(view);
    for (const ref of data?.image_references || []) {
      const target = [...document.querySelectorAll("[data-first-reference]")].find(item => item.dataset.firstReference === (ref.reference_id || ref.task_id));
      if (target) void requestFormalPreview(ref.preview_url, { owner: `creative-first:${view.projectId}`, priority: 1 }).then(url => {
        if (!target.isConnected) return;
        const image = document.createElement("img"); image.src = url; image.alt = ref.origin_label; target.replaceChildren(image);
      }).catch(() => { if (target.isConnected) target.textContent = "参考图暂时无法读取"; });
    }
    view.renderKey = renderKey;
    if (selection) {
      const target = [...document.querySelectorAll("[data-first-field]")].find(item => item.dataset.firstBlock === selection.block && item.dataset.firstField === selection.field);
      target?.focus({ preventScroll: true });
      if (target?.type !== "number") target?.setSelectionRange(selection.start, selection.end);
    }
  }
  const proposal = document.querySelector("#creative-first-proposal");
  proposal.hidden = !data?.proposal;
  if (data?.proposal?.workbench_parent_task_id) {
    proposal.innerHTML = workbenchParentReviewHtml(data.proposal.workbench_parent_task_id);
  } else if (data?.proposal) {
    const stale = firstCreativeProposalStale(view);
    const groups = data.proposal.groups || [];
    if (view.proposalDigest !== data.proposal.digest) {
      view.proposalDigest = data.proposal.digest;
      view.retainedBlocks = new Set(groups.map(group => group.block_id));
    }
    proposal.innerHTML = `<strong>${escapeHtml(stale ? "需求或初稿已更新，请重新预览建议。" : data.proposal.summary)}</strong><p>勾选要采用的段落；同一段落的关联修改一起采用，未选段落保持原样。</p><div id="creative-first-proposal-duration" class="creative-input-notice" role="status"></div>${Object.values(data.superseded_proposals || {}).includes(data.task?.task_id) ? '<p>本次预览已合并之前尚未落实的补充，替代旧修改建议。旧建议保留为历史，不能再直接采用。</p>' : ""}${groups.map(group => {
      const number = saved.findIndex(block => block.block_id === group.block_id) + 1;
      return `<section class="creative-first-change-group"><label><input type="checkbox" data-first-retain="${escapeHtml(group.block_id)}" ${view.retainedBlocks.has(group.block_id) ? 'checked' : ''} ${stale || view.busy ? 'disabled' : ''}>采用第 ${number} 段${escapeHtml(group.changes.map(change => change.field).join("、"))}</label>${group.changes.map(change => `<div class="creative-first-change"><h3>第 ${number} 段 · ${escapeHtml(change.field)}</h3><p><span>原文</span>${escapeHtml(change.before)}</p><p><span>建议</span>${escapeHtml(change.after)}</p></div>`).join('')}</section>`;
    }).join("")}<button type="button" class="primary-button" id="creative-first-apply" ${stale || view.busy || view.dirty.size || !view.retainedBlocks.size ? "disabled" : ""}>采用所选 ${view.retainedBlocks.size} 组</button><button type="button" class="text-button" id="creative-first-decline" ${stale || view.busy || view.dirty.size ? "disabled" : ""}>不采用本次建议</button>`;
    renderFirstProposalDuration(view);
  }
  const requirementUpdate = document.querySelector("#creative-first-requirement-update");
  requirementUpdate.hidden = !saved.length || Boolean(data?.proposal && !data?.answers_need_preview) || !(data?.unimplemented_answers?.length || data?.pending_answers?.length || data?.answers_need_preview);
  requirementUpdate.querySelector("p").textContent = confirmedBrief
    ? data?.proposal && !firstCreativeProposalStale(view)
      ? "仍有补充尚未纳入本次建议，需要另行预览；可先采用当前建议。"
      : "需求变更尚未应用到当前稿。"
    : "需求有变更，请返回需求页核对并确认后再修改脚本。";
  requirementUpdate.querySelector("button").disabled = !confirmedBrief || view.busy || running;
  renderWorkbenchCards();

}

async function refreshFirstCreative(view) {
  const epoch = state.formalFirstDraftEpoch;
  const active = () => epoch === state.formalFirstDraftEpoch
    && view.auth === state.agentAuth && view.workspaceId === state.agentWorkspaceId
    && formalRoute().page === "script" && formalRoute().projectId === view.projectId;
  if (view.reading) {
    if (view.readingEpoch === epoch) return view.reading;
    await view.reading;
    return active() ? refreshFirstCreative(view) : false;
  }
  view.readingEpoch = epoch;
  const run = async () => {
    let refreshed = false;
    if (!view.saving && !view.busy) {
      try {
        const cursor = view.data?.task?.task_id && view.data?.event_sequence
          ? `&since_task_id=${encodeURIComponent(view.data.task.task_id)}&after_sequence=${encodeURIComponent(view.data.event_sequence)}` : "";
        const known = view.data?.brief_marker
          ? `&known_revision=${encodeURIComponent(view.data.revision)}&known_brief_marker=${encodeURIComponent(view.data.brief_marker)}` : "";
        const response = await api(firstCreativePath(view) + cursor + known);
        if (!active()) return false;
        acceptFirstCreative(view, response);
        refreshed = true;
        view.readFailures = 0;
        if (view.readError) view.error = "";
        view.readError = false;
      } catch (error) {
        if (!active()) return false;
        view.readFailures = (view.readFailures || 0) + 1;
        view.readError = true; view.error = formalError(error);
      }
    }
    if (!active()) return false;
    renderFirstCreative(view);
    void maybeRecommendWorkbenchGeneration();
    window.clearTimeout(view.timer);
    if (firstCreativeVisible(view) && firstCreativePending(view))
      view.timer = window.setTimeout(() => refreshFirstCreative(view),
        Math.min(10000, 650 * (2 ** Math.min(view.readFailures || 0, 4))));
    return refreshed;
  };
  view.reading = run();
  try { return await view.reading; }
  finally { view.reading = null; }
}

async function loadFirstCreativeDraft(route) {
  const visible = Boolean(route.projectId && state.formalScript?.state === "empty" && route.panel !== "structure" && !route.aux);
  document.querySelector("#creative-first-draft").hidden = !visible;
  elements.creativeWorkspace.classList.toggle("creative-preformal", visible);
  document.querySelector('#creative-first-draft').classList.toggle('is-image-mode', route.panel === 'images');
  if (!visible) {
    if (route.projectId && state.formalScript?.state === 'empty') await refreshFirstCreative(firstCreativeView(route.projectId));
    return;
  }
  const view = firstCreativeView(route.projectId);
  const params = new URLSearchParams(window.location.search);
  view.activeBlockId = params.get('block_id') || view.activeBlockId;
  view.editMode = ['overview', 'shot', 'table'].includes(params.get('draft_view')) ? params.get('draft_view') : view.editMode;
  if (params.has('draft_tools')) document.querySelector('.creative-first-tools').open = params.get('draft_tools') === '1';
  view.renderKey = "";
  await refreshFirstCreative(view);
}

document.querySelector('.creative-first-tools').addEventListener('toggle', event => {
  if (!firstCreativeVisible(firstCreativeView())) return;
  const url = new URL(window.location.href); url.searchParams.set('draft_tools', event.target.open ? '1' : '0');
  window.history.replaceState({}, '', `${url.pathname}${url.search}`);
});

document.querySelector('#creative-first-search').addEventListener('input', event => {
  const view = firstCreativeView(); view.search = event.target.value; view.page = 1; renderFirstCreativeNavigation(view);
});
document.querySelector('#creative-first-draft').addEventListener('click', event => {
  const view = firstCreativeView();
  const jump = event.target.closest('[data-first-jump]');
  if (jump) { view.editMode = 'shot'; selectFirstCreativeBlock(view, jump.dataset.firstJump); }
  const mode = event.target.closest('[data-first-view]');
  if (mode) { view.editMode = mode.dataset.firstView; selectFirstCreativeBlock(view, view.activeBlockId); }
  const step = event.target.closest('[data-first-step]');
  if (step) {
    const blocks = view.data?.blocks || [], index = blocks.findIndex(block => block.block_id === view.activeBlockId);
    if (blocks[index + Number(step.dataset.firstStep)]) selectFirstCreativeBlock(view, blocks[index + Number(step.dataset.firstStep)].block_id);
  }
});
document.querySelector('#creative-first-align-duration').addEventListener('click', () => {
  const view = firstCreativeView(), target = view.data?.duration_status?.target_seconds;
  if (!Number.isFinite(target)) return;
  document.querySelector('.creative-first-tools').open = true;
  document.querySelector('#creative-first-assistant').open = true;
  document.querySelector('#creative-first-revision-scope').value = 'all';
  document.querySelector('#creative-first-revision-input').value = `将全稿总时长调整到已确认目标 ${target} 秒，按内容节奏分配，只调整时长；不要修改画面描述与声音；不要修改段落名称和顺序；保留事实和受保护段落。先展示调整建议供我核对。`;
  renderFirstCreativeScope(view);
  document.querySelector('#creative-first-revision-form').requestSubmit();
});

async function saveFirstCreative(view, operation = null) {
  if (view.saving) { await view.saving; if (operation) return saveFirstCreative(view, operation); return !view.error; }
  if (!view.data || view.conflict) return false;
  for (const pending of view.dirty.values()) {
    if (pending.field === "时长" && pending.value !== "" && firstCreativeSeconds(pending.value) === null) {
      view.saveError = "时长须为大于 0 的整数秒。原内容已保留，请修正此处输入后保存。";
      renderFirstCreative(view); return false;
    }
  }
  window.clearTimeout(view.saveTimer);
  const run = async () => {
    try {
      while (view.retry || view.dirty.size || operation) {
        const [key, pending] = view.retry ? [view.retry.key, view.retry.pending] : operation ? [null, { action: operation.action, block_id: operation.block_id }] : view.dirty.entries().next().value;
        if (!view.retry && key && view.data.blocks.find(block => block.block_id === pending.block_id)?.fields[pending.field] === pending.value) {
          view.dirty.delete(key);
          continue;
        }
        const body = view.retry?.body || { ...pending, expected_project_version: view.data.project_version,
          expected_revision: view.data.revision, expected_draft_revision: view.data.draft_revision,
          operation_key: pending.operation_key || crypto.randomUUID() };
        view.retry = { key, pending, body };
        const value = await api(firstCreativePath(view, "changes"), { method: "POST", body: JSON.stringify(body) });
        acceptFirstCreative(view, value);
        if (key && view.dirty.get(key) === pending) view.dirty.delete(key);
        view.retry = null;
        operation = null;
        view.error = "";
        view.saveError = "";
      }
      return true;
    } catch (error) {
      view.error = `${formalError(error)} 此处修改已保留。`;
      view.saveError = view.error;
      if (error.status === 400 || error.status === 422) view.retry = null;
      view.conflict = error.status === 409;
      return false;
    }
  };
  view.saving = run();
  renderFirstCreative(view);
  try { return await view.saving; }
  finally { view.saving = null; view.renderKey = ""; renderFirstCreative(view); }
}

document.querySelector("#creative-first-blocks").addEventListener("input", event => {
  const target = event.target.closest("[data-first-field]");
  if (!target || target.readOnly) return;
  const view = firstCreativeView();
  view.dirty.set(`${target.dataset.firstBlock}:${target.dataset.firstField}`, { action: "patch",
    block_id: target.dataset.firstBlock, field: target.dataset.firstField, value: target.value,
    operation_key: crypto.randomUUID() });
  window.clearTimeout(view.saveTimer);
  view.saveTimer = window.setTimeout(() => saveFirstCreative(view), 650);
  document.querySelector("#creative-first-status").textContent = "修改待保存";
  view.saveError = "";
  renderFirstCreativeScope(view);
  document.querySelector("#creative-first-save").disabled = false;
});
async function confirmFirstCreativeRegeneration(view) {
  if (view.regenerationDialog?.isConnected) return false;
  const opener = document.activeElement;
  const dialog = document.createElement('dialog');
  view.regenerationDialog = dialog;
  dialog.className = 'creative-regeneration-dialog';
  dialog.setAttribute('aria-labelledby', 'creative-regeneration-title');
  dialog.innerHTML = '<h2 id="creative-regeneration-title">重新起稿</h2><p>按当前已确认需求和所选模板生成完整新稿。新稿生成成功后替换当前初稿。</p><p>需要保留指定段落时，请返回使用智能修改。</p><form method="dialog"><button class="secondary-button" value="cancel" autofocus>保留当前稿</button><button class="primary-button" value="confirm">确认重新起稿</button></form>';
  document.body.appendChild(dialog);
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => {
    const confirmed = dialog.returnValue === 'confirm';
    dialog.remove(); view.regenerationDialog = null;
    if (opener?.isConnected) opener.focus({preventScroll: true});
    resolve(confirmed);
  }, {once: true}));
}

document.querySelector('#creative-first-confirmed-start').addEventListener('click', async () => {
  const view = firstCreativeView();
  if (!view.data || view.busy || firstCreativePending(view)) return;
  if (view.data.blocks?.length) {
    if (!await confirmFirstCreativeRegeneration(view)) return;
    if (view !== firstCreativeView() || view.busy || firstCreativePending(view)) return;
    if (!await saveFirstCreative(view)) return;
  }
  view.busy = true; view.error = ""; renderFirstCreative(view);
  try {
    view.startRetry = view.startRetry || {action: "generate", body: {
      expected_project_version: view.data.project_version, expected_revision: view.data.revision,
      instruction: "依据本项目已确认的需求与创作标准，创作一份完整脚本初稿。保留事实、流程和约束。",
      use_confirmed_brief: true, operation_key: crypto.randomUUID(),
    }};
    acceptFirstCreative(view, await api(firstCreativePath(view, "generate"), {method: "POST", body: JSON.stringify(view.startRetry.body)}));
    if (document.querySelector("#workbench-recommend-after-generation").checked && view.data.task?.task_id) rememberWorkbenchRecommendation("creative", view.data.task.task_id);
    view.startRetry = null;
  } catch (error) {
    if (error.status && error.status < 500) view.startRetry = null;
    view.error = formalError(error);
  } finally { view.busy = false; await refreshFirstCreative(view); }
});
document.querySelector("#creative-first-save").addEventListener("click", () => saveFirstCreative(firstCreativeView()));
document.querySelector("#creative-first-plan-link").addEventListener("click", async event => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  const view = firstCreativeView();
  if (firstCreativePending(view) || !await saveFirstCreative(view)) return;
  const query = { panel: "structure", ...(view.data?.blocks?.length ? { creative: "1" } : {}) };
  navigateFormal(formalPath("script", view.projectId, query));
});
document.querySelector("#creative-first-reload").addEventListener("click", async () => {
  const view = firstCreativeView();
  if (!await refreshFirstCreative(view)) return;
  view.conflict = false;
  view.retry = null;
  for (const pending of view.dirty.values()) pending.operation_key = crypto.randomUUID();
  view.error = "已读取最新内容，此处修改仍保留；核对后可点击保存进度。";
  view.renderKey = "";
  renderFirstCreative(view);
});
document.querySelector("#creative-first-draft").addEventListener("click", async event => {
  const target = event.target.closest("[data-first-action], [data-first-delete]");
  if (!target) return;
  const view = firstCreativeView();
  if (!await saveFirstCreative(view)) return;
  await saveFirstCreative(view, { action: target.dataset.firstAction || "delete", block_id: target.dataset.firstDelete });
});
document.querySelector("#creative-first-blocks").addEventListener("change", async event => {
  const view = firstCreativeView();
  if (event.target.matches("[data-first-select]")) {
    const id = event.target.dataset.firstSelect;
    if (event.target.checked) view.selected.add(id); else view.selected.delete(id);
    document.querySelector("#creative-first-revision-scope").value = "selected";
    renderFirstCreativeScope(view);
    renderFirstCreativeNavigation(view);
  } else if (event.target.matches("[data-first-protect]")) {
    const id = event.target.dataset.firstProtect;
    const checked = event.target.checked;
    if (!await saveFirstCreative(view) || view.busy) return;
    view.busy = true;
    try {
      const selected = new Set(view.data.protected_block_ids || []);
      if (checked) selected.add(id); else selected.delete(id);
      acceptFirstCreative(view, await api(firstCreativePath(view, "protection"), { method: "POST", body: JSON.stringify({
        expected_project_version: view.data.project_version, expected_revision: view.data.revision,
        protected_block_ids: [...selected], operation_key: crypto.randomUUID(),
      }) }));
      view.error = "";
    } catch (error) { view.error = formalError(error); }
    finally { view.busy = false; view.renderKey = ""; renderFirstCreative(view); }
  }
});
document.querySelector("#creative-first-revision-scope").addEventListener("change", () => renderFirstCreativeScope(firstCreativeView()));
document.querySelector("#creative-first-revision-form").addEventListener("submit", async event => {
  event.preventDefault();
  const view = firstCreativeView();
  if (view.busy || !await saveFirstCreative(view)) return;
  const selected = document.querySelector("#creative-first-revision-scope").value === "all"
    ? view.data.blocks.map(block => block.block_id) : document.querySelector("#creative-first-revision-scope").value === "current" ? [view.activeBlockId].filter(Boolean) : [...view.selected];
  if (!selected.length) { view.error = "请先勾选允许修改的段落。"; renderFirstCreative(view); return; }
  view.busy = true;
  view.error = "正在分析本次修改…";
  renderFirstCreative(view);
  try {
    acceptFirstCreative(view, await api(firstCreativePath(view, "revisions"), { method: "POST", body: JSON.stringify({
      expected_project_version: view.data.project_version, expected_revision: view.data.revision,
      selected_block_ids: selected, instruction: document.querySelector("#creative-first-revision-input").value,
      operation_key: crypto.randomUUID(),
    }) }));
    view.error = "";
  } catch (error) { view.error = formalError(error); }
  finally { view.busy = false; await refreshFirstCreative(view); }
});
document.querySelector('#creative-first-proposal').addEventListener('change', event => {
  const checkbox = event.target.closest('[data-first-retain]'); if (!checkbox) return;
  const view = firstCreativeView();
  if (checkbox.checked) view.retainedBlocks.add(checkbox.dataset.firstRetain);
  else view.retainedBlocks.delete(checkbox.dataset.firstRetain);
  const button = document.querySelector('#creative-first-apply');
  button.textContent = `采用所选 ${view.retainedBlocks.size} 组`;
  button.disabled = !view.retainedBlocks.size || Boolean(view.dirty.size) || view.busy || firstCreativeProposalStale(view);
  renderFirstProposalDuration(view);
});
document.querySelector("#creative-first-proposal").addEventListener("click", async event => {
  if (event.target.closest('[data-first-proposal-duration-adjust]')) {
    document.querySelector('#creative-first-align-duration').click();
    return;
  }
  const decline = Boolean(event.target.closest("#creative-first-decline"));
  if (!decline && !event.target.closest("#creative-first-apply")) return;
  const view = firstCreativeView();
  if (view.busy || !view.data.proposal || view.data.proposal.workbench_parent_task_id || firstCreativeProposalStale(view) || (!decline && !view.retainedBlocks?.size) || !await saveFirstCreative(view)) return;
  view.busy = true;
  renderFirstCreative(view);
  try {
    acceptFirstCreative(view, await api(firstCreativePath(view, "revisions/confirm"), { method: "POST", body: JSON.stringify({
      task_id: view.data.task.task_id, proposal_digest: view.data.proposal.digest,
      retained_block_ids: decline ? [] : [...view.retainedBlocks],
    }) }));
    view.error = "";
  } catch (error) { view.error = formalError(error); }
  finally { view.busy = false; await refreshFirstCreative(view); }
});
document.querySelector("#creative-first-requirement-update").addEventListener("click", async event => {
  if (!event.target.closest("[data-first-answer-continue]")) return;
  const view = firstCreativeView();
  if (view.busy || !await saveFirstCreative(view)) return;
  view.busy = true;
  renderFirstCreative(view);
  try {
    acceptFirstCreative(view, await api(firstCreativePath(view, "answers/continue"), { method: "POST", body: "{}" }));
    view.error = "";
  } catch (error) { view.error = formalError(error); }
  finally { view.busy = false; await refreshFirstCreative(view); }
});

function renderCreativeReadState(mode = '', message = '') {
  const host = document.querySelector('#creative-workspace-read-state');
  elements.creativeWorkspace.classList.toggle('is-reading-current', Boolean(mode));
  host.hidden = !mode;
  if (mode) host.innerHTML = mode === 'loading'
    ? '<strong>正在读取当前初稿与版本</strong><p>读取完成后显示已保存内容。</p>'
    : `<strong>当前初稿与版本读取失败</strong><p>${escapeHtml(message || '暂时无法读取已保存内容。')} 当前未更改稿件，请重新读取。</p><button type="button" class="secondary-button compact" data-creative-read-retry>重新读取当前稿</button>`;
}

document.querySelector('#creative-workspace-read-state').addEventListener('click', event => {
  if (event.target.closest('[data-creative-read-retry]')) void renderFormalRoute();
});

async function loadCreativeWorkspace(route = formalRoute()) {
  let current = creativeState();
  if (current.projectId !== (route.projectId || "")) {
    captureCreativeInputs();
    releaseFormalImagePreviews();
    state.formalCreative = null;
    current = creativeState();
    current.projectId = route.projectId || "";
    const imageFlow = document.querySelector("#creative-image-flow");
    imageFlow.replaceChildren(); imageFlow.hidden = true;
    const firstImageFlow = document.querySelector("#creative-first-image-flow"); firstImageFlow.replaceChildren(); firstImageFlow.hidden = true;
    state.formalImageWorkspace = null;
    state.formalImages = null;
    state.formalDefaultImages = null;
    state.formalDefaultImageContext = "";
    state.formalDefaultImageSelections.clear();
    state.formalDefaultLibrarySelected.clear();
    elements.formalScriptInstruction.value = "";
    elements.formalScriptSaveReason.value = "";
    elements.creativeShotSearch.value = "";
  }
  const request = ++current.navigationRequest;
  const awaitingCurrent = Boolean(route.projectId && state.formalScriptAvailable && state.agentAuth && state.agentWorkspaceId
    && (!current.loaded || state.formalScript?.state === 'empty' && !firstCreativeView(route.projectId).data));
  renderCreativeReadState(awaitingCurrent ? 'loading' : '');
  syncCreativeVisibility(route);
  if (!current.loaded && !current.loadPromise) {
    const loading = (async () => {
      if (route.projectId) void refreshCreativeOverview(route.projectId);
      await loadFormalScript(route.projectId);
      if (creativeState() !== current || current.loadPromise !== loading || !state.formalScript) return;
      syncCreativeContext(state.formalScript);
      current.loaded = true;
    })().finally(() => { if (current.loadPromise === loading) current.loadPromise = null; });
    current.loadPromise = loading;
  }
  if (current.loadPromise) await current.loadPromise;
  if (creativeState() !== current || request !== current.navigationRequest || formalRoute().page !== "script") return;
  if (!current.loaded) {
    if (awaitingCurrent) renderCreativeReadState('error', state.formalScriptReadError);
    return;
  }
  route = formalRoute();
  if (!route.explicitPanel && state.formalScript?.state === "empty") {
    const url = new URL(window.location.href);
    url.searchParams.set("panel", "script");
    window.history.replaceState({}, "", `${url.pathname}${url.search}`);
    route = formalRoute();
  }
  const params = new URLSearchParams(window.location.search);
  const requestedShotId = params.get("shot_id");
  const legacyShot = params.get("focus_shot");
  if (requestedShotId && requestedShotId !== current.shotId) selectCreativeShot(requestedShotId, { updateUrl: false });
  else if (legacyShot && !current.locationUnresolved && current.legacyFocusKey !== legacyShot) {
    current.legacyFocusKey = legacyShot;
    const shot = state.formalScript?.shots?.find(item => String(item.shot_no) === legacyShot);
    if (shot?.shot_id) selectCreativeShot(shot.shot_id, { updateUrl: false });
  }
  syncCreativeVisibility(route);
  await loadFirstCreativeDraft(route);
  if (creativeState() !== current || request !== current.navigationRequest || formalRoute().page !== 'script') return;
  const initialReadFailed = state.formalScript?.state === 'empty' && !firstCreativeView(route.projectId).data;
  renderCreativeReadState(initialReadFailed ? 'error' : '', initialReadFailed ? firstCreativeView(route.projectId).error : '');
  if (initialReadFailed) return;
  if (state.formalScript?.state === "empty" && route.panel !== "structure") {
    let recommendation; try { recommendation = JSON.parse(localStorage.getItem(workbenchRecommendationKey()) || "null"); } catch (_) {}
    if (route.panel === "images" || recommendation?.status === "started") void restoreImageBatch(route.projectId);
  }
  const key = `${current.versionId}:${state.formalScript?.project_version || ""}`;
  if (route.aux === "materials") {
    if (current.materialsKey !== key || !state.formalImages) {
      await loadFormalImages(route.projectId);
      current.materialsKey = key;
    } else if (["library", "recommended"].includes(params.get("pick"))) {
      const view = params.get("pick");
      if (!state.formalDefaultImages || state.formalDefaultImageView !== view || view === "recommended" && state.formalDefaultImageShotNo)
        await loadFormalDefaultImages({ view, ...(view === "recommended" ? { shotNo: "", matchScope: "", query: "", filters: {}, page: 1 } : {}) });
    }
  } else if (route.panel === "structure") {
    const planKey = `${key}:${params.get("mode") || "standard"}`;
    if (current.planKey !== planKey || state.formalPlanProjectId !== route.projectId) { current.planKey = planKey; await loadFormalPlan(route.projectId); }
    else if (!elements.creativeCurrentStructure.hidden) renderCreativeCurrentStructure(state.formalScript);
  } else if (state.formalScript?.state !== 'empty' && (current.imageKey !== key || !state.formalImageWorkspace)) {
    await loadFormalShotImages(route.projectId);
    current.imageKey = key;
  }
  if (creativeState() !== current || request !== current.navigationRequest || formalRoute().page !== "script") return;
  syncCreativeVisibility(formalRoute());
  renderCreativeOutline();
  if (formalRoute().panel === "script" && document.querySelector("#creative-assistant")?.open) {
    void restoreImageBatch(route.projectId);
  }
  if (params.get("action") === "save") openCreativeTool("save");
  const field = params.get("field") || "";
  if (field && requestedShotId && route.panel === "script") selectCreativeShot(requestedShotId, { updateUrl: false, field, focus: true });
  const scroll = current.scroll.get(formalSurface());
  if (scroll !== undefined) window.scrollTo({ top: scroll, behavior: "instant" });
  if (route.panel === "images" && !route.aux && legacyShot && !current.locationUnresolved) {
    current.imageDetail = true; syncCreativeShotVisibility();
    const index = state.formalImageWorkspace?.shots?.findIndex(shot => shot.shot_id === current.shotId) ?? -1;
    if (index >= 0) focusFormalImageShot(index, { select: false });
  }
}

async function changeCreativePanel(panel, { aux = "", pick = "", shotId = creativeState().shotId } = {}) {
  const route = formalRoute();
  const retained = new URLSearchParams(window.location.search);
  const context = Object.fromEntries(["revision_task_id", "revision_shot", "analysis_task_id", "feedback_kind", "field", "supplement_image_key", "mode", "block_id", "draft_view", "draft_tools", "creative"].filter(key => retained.has(key)).map(key => [key, retained.get(key)]));
  const query = { ...context, panel, ...(shotId ? { shot_id: shotId } : {}), ...(aux ? { aux } : {}), ...(pick ? { pick } : {}) };
  if (aux) {
    query.return_to = "shot-images";
    const shot = creativeCurrentShot();
    if (shot) query.focus_shot = String(shot.shot_no);
  }
  return navigateFormal(formalPath("script", route.projectId, query));
}

function openCreativeTool(tool) {
  if (tool === "assistant") return openWorkbenchAssistant();
  const id = tool === "assistant" ? "creative-assistant" : tool === "save" ? "creative-save" : "creative-script-records";
  let panel = document.getElementById(id);
  if (tool === "records") panel = document.querySelector(".ui00-script-versions");
  if (formalSurface() !== "script") {
    const projectId = formalRoute().projectId;
    return changeCreativePanel("script").then(() => formalRoute().projectId === projectId ? openCreativeTool(tool) : false);
  }
  if (!panel) return;
  if (tool === "assistant") { syncCreativeAssistantCopy(); void restoreImageBatch(formalRoute().projectId); }
  panel.open = true;
  if (tool === "save" && !elements.formalScriptSaveReason.value.trim()) {
    const instruction = state.formalScriptProposalInstruction || elements.formalScriptInstruction.value.trim();
    elements.formalScriptSaveReason.value = instruction.slice(0, 500) || "保存本次镜头编辑";
  }
  panel.scrollIntoView({ block: "nearest" });
  const confirmation = tool === "save" && state.formalScriptRevisionProposal
    ? panel.querySelector("[data-confirm-script-revision]") : null;
  (tool === "assistant" ? elements.formalScriptInstruction : tool === "save" ? confirmation || elements.formalScriptSaveReason : panel.querySelector("summary"))?.focus({ preventScroll: true });
  syncCreativePrimaryAction();
}

function syncCreativeAssistantCopy() {
  const images = document.querySelector("#creative-assistant-kind").value === "images";
  elements.formalScriptInstruction.placeholder = images
    ? "例如：第 2 镜换成工厂全景，避免人物特写。"
    : "例如：第 2 镜旁白更自然，保留产品名称。";
  const choices = images
    ? [["换成全景", "换成更能说明场景的全景图片", "images"], ["突出产品", "配图突出产品主体", "images"], ["减少重复配图", "减少重复配图，保留已确认的画面", "images"]]
    : [["减少航拍", "减少航拍镜头", "structure"], ["增加特写", "增加特写镜头", "structure"], ["旁白更自然", "旁白更自然", "copy"], ["字幕更简洁", "字幕更简洁", "copy"], ["减少重复", "减少重复内容", "copy"], ["减少套话", "减少常用套话和AI味", "copy"], ["节奏更紧凑", "节奏更紧凑", "copy"]];
  document.querySelector("#formal-script-quick-feedback").innerHTML = '<legend>快捷意见</legend>' + choices.map(([label, instruction, kind]) =>
    `<button class="secondary-button compact" type="button" data-script-quick-feedback="${escapeHtml(instruction)}" data-feedback-kind="${kind}">${label}</button>`).join("");
}

async function openCreativeLibrary() {
  await openCreativeImagePicker("library");
}

async function openCreativeBatchImages(options = {}) {
  return startWorkbenchImageBatch(options);
}

function formalError(error) {
  const code = error?.payload?.detail?.code;
  if (error?.status === 403 && typeof error?.payload?.detail === "string"
      && /^本地会话已失效/.test(error.payload.detail)) {
    return "本地会话已失效，请刷新页面后继续。已保存的稿件和任务记录仍保留。";
  }
  const detailMessage = String(error?.payload?.detail?.message || "").trim();
  const incompleteSound = code === 'FORMAL_GENERATION_BLOCKED' && detailMessage.match(/^镜\s*(\d+)\s*声音不能为空$/);
  if (incompleteSound) return `第 ${incompleteSound[1]} 镜尚未生成声音安排，本次未创建版本。原稿已保留，可重新生成规划后再试。`;
  if (["UPLOAD_REJECTED", "FILE_TOO_LARGE", "UPLOAD_LIMIT_EXCEEDED", "INPUT_TAMPERED"].includes(code) && detailMessage) {
    const fileIssue = detailMessage.match(/^(.+): ([A-Z_]+)$/);
    const readingHelp = {
      UNSUPPORTED_TYPE: "此格式不支持，请改用 PDF、DOCX、XLSX、PPTX、TXT、MD、JPG、JPEG、PNG 或 WebP。",
      TYPE_MISMATCH: "文件扩展名与实际内容不一致，请从原软件重新导出后选择文件。",
      MALFORMED_PDF: "PDF 不完整或无法读取，请重新导出可打开的 PDF 后上传。",
      ENCRYPTED_PDF: "PDF 已加密，请上传可直接读取的未加密副本。",
      MALFORMED_OFFICE: "Office 文件不完整，请从原软件重新另存后上传。",
      ENCRYPTED_OFFICE: "Office 文件已加密，请上传可直接读取的未加密副本。",
      MALFORMED_IMAGE: "图片无法打开，请重新导出完整的 JPG、PNG 或 WebP。",
      EXTRACTION_FAILED: "未能读取正文，请检查文件是否完整且有内容；文本文件请保存为 UTF-8 后重新选择。",
    };
    if (fileIssue && readingHelp[fileIssue[2]]) return `${fileIssue[1]}：${readingHelp[fileIssue[2]]}`;
    return `资料读取失败：${detailMessage}`;
  }
  if (["confirmed_brief_needs_clarification", "PLAN_INPUTS_STALE"].includes(code) && detailMessage) return detailMessage;
  if (["IMAGE_SCOPE_MISMATCH", "IMAGE_INPUT_INVALID", "IMAGE_EDIT_UNAVAILABLE", "DRAFT_REVISION_CONFLICT", "IMAGE_SHOT_CLARIFICATION_REQUIRED", "CREATIVE_BRIEF_FLOW_CONFLICT"].includes(code) && detailMessage) return detailMessage;
  return {
    WORKBENCH_CONFIRMATION_REQUIRED: "这条建议属于联合修改，当前稿未改变。请在创作助手核对联合建议后一起采用。",
    FORMAL_BRIEF_DRIFT: "已确认需求与当前稿依据不同。旧稿保留，可放弃全部草稿后在结构规划中按新需求重新规划；刷新不会切换旧稿依据。",
    HERMES_TRACE_MISMATCH: "当前版本的图片来源证明不完整，尚未导出文件。请核对配图来源；刷新不能补齐来源证明。",
    IMAGE_ADOPTION_PROGRESS_PENDING: "图片已进入项目素材；选图进度未能保存。请核对当前稿后再次采用，原图与结果仍保留。",
    IMAGE_PROVIDER_UNAVAILABLE: "图片账号尚未配置，文字创作与现有图库仍可使用。",
    IMAGE_PROGRESS_CONFLICT: "另一处已更新选图进度，当前输入仍保留。请查看最新进度，再按当前要求重新生成建议。",
    ASSISTANT_LOCKED: "本次范围中有镜头已保护，当前选择仍保留。请重新生成建议。",
    ASSET_PACKAGE_UNAVAILABLE: "当前类别尚未配置可用模板与图库，请在模板管理中补充后重试",
    ASSET_PACKAGE_RESOLVER_INVALID: "当前模板与图库的绑定暂不可用，请检查该项目已选的模板版本后重试",
    ASSET_PACKAGE_CATEGORY_MISMATCH: "项目类别与所选图库不一致，请核对模板选择",
    ASSET_PACKAGE_DRIFT: "已选模板内容发生变化，请恢复该模板版本或重新选择模板后重试",
    ASSET_PACKAGE_BLOB_DRIFT: "图库原图缺失或内容已变化，请恢复原图后重试",
    IMAGE_VERSION_AUDIT_FAILED: "配图版本未通过文件校验，本次没有创建新版本。已保存的选图进度保留。",
    AUTH_SESSION_INVALID: "登录已失效，请通过工作台重新进入。",
    PERMISSION_DENIED: "你没有访问这个项目的权限，请返回项目列表选择项目。",
    CROSS_PROJECT_DENIED: "当前资料不属于这个项目，请重新进入项目后再试。",
    NOT_FOUND: "未找到所需项目或资料，请返回项目列表后重试。",
    FORMAL_BRIEF_ADAPTER_UNAVAILABLE: "Brief 功能暂不可用，请稍后重试或联系管理员。",
    FORMAL_BRIEF_INVALID: "这份 Brief 暂时不能继续处理，请检查资料状态与冲突。",
    BRIEF_CATEGORY_REQUIRED: "请在 Brief 中补充脚本类型，并核对冲突后确认。",
    BRIEF_UNDERSTANDING_FAILED: "资料智能理解暂时不可用，本次没有导入，请稍后重试。",
    BRIEF_UNDERSTANDING_INVALID: "资料理解结果未通过来源校验，本次没有导入。",
    BRIEF_UNDERSTANDING_EVIDENCE_INVALID: "资料理解的来源与原文不一致，本次没有导入。",
    DUPLICATE_UPLOAD: "同一批资料中有内容完全相同的文件，请只保留一份。",
    DUPLICATE_UPLOAD_CONFLICT: "重复资料与项目已冻结记录不一致，请刷新后再试。",
    FORMAL_PLAN_UNAVAILABLE: "规划暂不可用，请稍后重试或联系管理员。",
    PLAN_EVENT_INVALID: "旧规划的绑定证据不一致，不能继续确认。原稿已保留，请返回规划页按当前资料重新规划。",
    PLAN_ARTIFACT_DRIFT: "旧规划文件已变化，不能继续确认。原稿已保留，请返回规划页重新规划。",
    TEMPLATE_FILE_INVALID: "请选择不超过 50 MB 的 Excel 模板。",
    AI_SPEC_NOT_READY: "请先在原文件同步并复核 AI 规范表，再导入。",
    TEMPLATE_IMPORT_CONFLICT: "此模板有未完成的导入，请检查后重试。",
    TEMPLATE_IMPORT_FAILED: "导入失败，请检查模版、创意库和 AI 规范表。",
    TEMPLATE_UNAVAILABLE: "模板暂不可用，请重新读取或导入模板。",
    TEMPLATE_REF_INVALID: "模板版本无效，请重新选择。",
    FORMAL_VISION_PROVIDER_FAILED: "自动图片理解未完成，原图仍保持待处理；请重试或改为人工理解。",
    FORMAL_GENERATION_BLOCKED: "脚本生成结果与已确认规划不一致，本次没有创建版本；请重新生成规划后再试。",
    CREATIVE_PLAN_MAPPING_REQUIRED: "当前镜数无法独立容纳已选初稿，请调整镜数或初稿后重新规划。",
    FORMAL_GENERATION_AUDIT_FAILED: "生成脚本未通过机器审计，本次没有创建版本；请重新生成规划后再试。",
    FORMAL_EXECUTION_FAILED: "本次操作已停止，请查看任务记录并联系管理员核对。",
    RECOVERY_RECONCILIATION_REQUIRED: "原操作已停止，现有记录不足以确认完成。请核对当前脚本和版本记录后重新提交。",
    SCRIPT_TYPE_UNSUPPORTED: "需求中的脚本类型尚不支持。请返回需求页补充一个可用的活动类型后重试。",
    BRIEF_DURATION_INVALID: "需求中的成片时长无法执行。请补充明确的正整数秒，例如 60 秒。",
    CREATIVE_STANDARD_MISSING: "请先完成并确认需求与项目创作标准卡。",
    CONFIRMED_BRIEF_REQUIRED: "请先完成并确认需求。",
    CURRENT_BRIEF_MISSING: "请先完成并确认需求与项目创作标准卡。",
    FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED: "请先在项目图片页完成待处理图片的人工理解。",
    IMAGE_ARTIFACT_DRIFT: "图片资料状态已变化，请刷新后重试。",
    IMAGE_ANALYSIS_ARTIFACT_DRIFT: "图片理解记录已变化，请刷新后重试。",
    INTEGRITY_ERROR: "图片资料未通过完整性检查，请刷新或补充资料。",
    PLAN_CONFIRMATION_REQUIRED: "请先核对规划，再确认创建脚本版本。",
    PLAN_NOT_WAITING_CONFIRMATION: "这份规划已不处于待确认状态，请刷新后重试。",
    FORMAL_SCRIPT_UNAVAILABLE: "脚本编辑与版本功能暂不可用，请稍后重试或联系管理员。",
    DRAFT_REVISION_CONFLICT: "草稿已在其他页面发生变化，已为你读取最新内容。",
    DRAFT_NOT_CHANGED: "当前脚本还没有需要保存的修改。",
    DRAFT_NOT_SAVED: "当前稿仍有未保存修改，请先保存为版本，或撤销修改后再下载复核稿。",
    DRAFT_VERSION_CONFLICT: "当前脚本版本已变化，已为你读取最新版本。",
    DRAFT_INVALID: "这项修改不符合脚本字段要求，请检查后重试。",
    IMAGE_CONFIRMATION_REQUIRED: "本次尚未创建版本。文字和选图进度已保留，请逐镜补图并确认后再保存；刷新不会代替配图确认。",
    INSTRUCTION_AMBIGUOUS: "请明确镜号、字段和新内容，例如“把第 2 镜声音修改为现场掌声”。",
    SCRIPT_VERSION_AUDIT_FAILED: "草稿未通过版本校验，请修正提示的问题后再保存。",
    FORMAL_REVIEW_UNAVAILABLE: "项目经理审核暂不可用，请稍后重试或联系管理员。",
    REVIEW_PREREQUISITE_BLOCKED: "请先完成逐镜图片确认和当前版本机器检查。",
    REVIEW_NEW_VERSION_REQUIRED: "该问题已退回，请在脚本或图片区保存新版本后重新审核。",
    FINAL_REVIEW_NOT_READY: "仍有待处理或已退回的问题，暂不能完成整片确认。",
    REVIEW_INVALID: "审核状态不完整或已变化，请刷新后重新核对。",
    FINAL_CONFIRMATION_EXISTS: "当前版本已经完成整片确认，不能重复提交。",
    DELIVERY_NOT_READY: "完成整片人工确认后才能导出 Excel。",
    DELIVERY_ARTIFACT_DRIFT: "交付文件与当前确认版本不一致，请刷新或重新保存版本。",
    ASSET_PACKAGE_DRIFT: "原版本的来源文件缺失或已变化，请联系管理员恢复原文件后再保存。",
    ARTIFACT_INVALID: "当前版本的业务证据不可读取，请刷新或重新生成版本。",
    IDEMPOTENCY_CONFLICT: "这次保存请求与之前的内容不一致，请重新提交。",
    PROJECT_STATE_CHANGED: "项目资料已更新，已读取最新状态，请确认后再次操作。",
    INVALID_INPUT: "请检查所选文件和必填内容后重试。",
    PROJECT_VERSION_CONFLICT: "项目资料已更新，已读取最新状态，请确认后再次操作。",
    SCRIPT_DRAFT_NOT_SAVED: "当前脚本有未保存修改。请到“脚本与版本”保存为新版本或放弃修改，再重新规划；缺图时可先继续配图并保存进度。",
    WORKSPACE_VERSION_CONFLICT: "项目列表已更新，请再次提交。",
    PROJECT_ARCHIVED: "项目已归档，恢复前只能查看已有资料和历史版本。",
    PROJECT_ALREADY_ARCHIVED: "项目已经归档，请刷新页面。",
    PROJECT_NOT_ARCHIVED: "项目当前不是归档状态，请刷新页面。",
    PROJECT_BUSY: "项目仍有进行中或等待确认的任务，请先处理后再归档。",
    PROJECT_ARCHIVE_EXPIRED: "30 天恢复窗口已结束；项目仍保留可读，永久清理尚未开放。",
    PROJECT_ARCHIVE_EVIDENCE_MISSING: "归档证据缺失，不能自动恢复，请联系管理员核对。",
    MUTATION_PURGE_UNAVAILABLE: "永久物理清理尚未开放。",
  }[code] || "操作没有完成，请刷新页面后重试。";
}

function formalProject() {
  const route = formalRoute();
  return state.formalProjects.find((project) => project.project_id === route.projectId) || null;
}

function formalProjectUpdatedAt(project) {
  return project.summary?.updated_at || project.updated_at || project.created_at || "";
}

function renderFormalProjects() {
  if (!elements.formalProjectList) return;
  const query = elements.formalProjectSearch.value.trim().toLocaleLowerCase();
  const matched = state.formalProjects.filter((project) => !query || String(project.name || "").toLocaleLowerCase().includes(query))
    .sort((left, right) => (Date.parse(formalProjectUpdatedAt(right)) || 0) - (Date.parse(formalProjectUpdatedAt(left)) || 0)
      || String(left.project_id).localeCompare(String(right.project_id)));
  const projects = matched.filter((project) => project.status !== "archived");
  const archived = matched.filter((project) => project.status === "archived");
  const pageSize = 12;
  const visiblePage = (items, key) => {
    const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
    state[key] = Math.max(1, Math.min(state[key] || 1, totalPages));
    return items.slice((state[key] - 1) * pageSize, state[key] * pageSize);
  };
  const pagination = (items, key) => items.length > pageSize
    ? `<nav class="list-pagination" aria-label="${key === "formalProjectsPage" ? "项目分页" : "归档项目分页"}"><button class="secondary-button compact" type="button" data-project-page="${key}" data-page="${state[key] - 1}"${state[key] === 1 ? " disabled" : ""}>上一页</button><span>第 ${state[key]} / ${Math.ceil(items.length / pageSize)} 页 · 共 ${items.length} 个</span><button class="secondary-button compact" type="button" data-project-page="${key}" data-page="${state[key] + 1}"${state[key] * pageSize >= items.length ? " disabled" : ""}>下一页</button></nav>` : "";
  const readable = state.formalBriefAvailable && state.agentAuth && state.agentWorkspaceId && state.formalProjectsLoaded && !state.formalProjectsError;
  elements.formalProjectCount.textContent = readable ? `${projects.length} 个` : "—";
  elements.formalArchivedCount.textContent = readable ? `${archived.length} 个` : "—";
  if (!state.formalBriefAvailable) {
    elements.formalProjectNew.setAttribute("aria-disabled", "true");
    elements.formalProjectNew.tabIndex = -1;
    elements.formalProjectList.innerHTML = `<div class="workbench-empty">${escapeHtml(state.formalSetupError || "项目工作区暂不可用，请稍后重试或联系管理员。")}</div>`;
    elements.formalArchivedList.innerHTML = '<div class="workbench-empty">归档项目暂不可用。</div>';
    return;
  }
  if (!state.agentAuth || !state.agentWorkspaceId) {
    elements.formalProjectNew.setAttribute("aria-disabled", "true");
    elements.formalProjectNew.tabIndex = -1;
    elements.formalProjectList.innerHTML = `<div class="workbench-empty">${state.formalOwnerRecoveryRequired ? "请在上方确认重新进入原工作区。" : "请先在上方启用工作区，再创建项目。"}</div>`;
    elements.formalArchivedList.innerHTML = '<div class="workbench-empty">请先进入工作区。</div>';
    return;
  }
  elements.formalProjectNew.removeAttribute("aria-disabled");
  elements.formalProjectNew.removeAttribute("tabindex");
  if (state.formalProjectsError) {
    elements.formalProjectList.innerHTML = `<div class="workbench-empty" role="status"><p>项目读取失败：${escapeHtml(state.formalProjectsError)}</p><button class="secondary-button compact" type="button" data-projects-retry>重新读取项目</button></div>`;
    elements.formalArchivedList.innerHTML = '<div class="workbench-empty">归档项目尚未读到，请重新读取项目。</div>';
    return;
  }
  if (!state.formalProjectsLoaded) {
    elements.formalProjectList.innerHTML = '<p role="status">正在读取项目…</p>';
    elements.formalArchivedList.innerHTML = '<p role="status">正在读取归档项目…</p>';
    return;
  }
  if (!projects.length) {
    elements.formalProjectList.innerHTML = `<div class="workbench-empty">${query ? '没有匹配的项目，请调整搜索条件。' : '暂无项目。新建项目后会在这里显示。'}</div>`;
  } else {
    elements.formalProjectList.innerHTML = visiblePage(projects, "formalProjectsPage").map((project) => `
      <article class="ui00-project-row"><div><strong>${escapeHtml(project.name || "未命名项目")}</strong><small>进行中 · ${escapeHtml(project.summary?.stage || "状态待核对")}</small><small>更新于 ${escapeHtml(taskDisplayTime(formalProjectUpdatedAt(project)))}</small></div><a class="secondary-button compact" href="${escapeHtml(project.summary?.next_action?.href || formalPath("overview", project.project_id))}" data-list-page>${escapeHtml(project.summary?.next_action?.label || "打开项目")}</a></article>
    `).join("") + pagination(projects, "formalProjectsPage");
  }
  elements.formalArchivedList.innerHTML = archived.length ? visiblePage(archived, "formalArchivedPage").map((project) => `
    <article class="ui00-project-row is-archived"><div><strong>${escapeHtml(project.name || "未命名项目")}</strong><small>已归档 · 资料保持可读</small></div><a class="secondary-button compact" href="${formalPath("overview", project.project_id)}" data-route="overview">查看／恢复</a></article>
  `).join("") + pagination(archived, "formalArchivedPage") : `<div class="workbench-empty">${query ? "没有匹配的归档项目，请调整搜索条件。" : "暂无归档项目。"}</div>`;
}

async function loadFormalProjects() {
  if (!state.formalBriefAvailable || !state.agentAuth || !state.agentWorkspaceId) {
    renderFormalProjects();
    return;
  }
  const request = state.formalProjectsRequest = (state.formalProjectsRequest || 0) + 1;
  state.formalProjectsError = "";
  renderFormalProjects();
  try {
    const workspace = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}`);
    state.formalWorkspaceVersion = workspace.workspace_version;
    const response = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/projects?summaries=true`);
    if (request !== state.formalProjectsRequest) return;
    state.formalProjects = response.items || [];
    state.formalProjectsLoaded = true;
    renderFormalProjects();
  } catch (error) {
    if (request !== state.formalProjectsRequest) return;
    state.formalProjectsError = formalError(error);
    renderFormalProjects();
  }
}

function showFormalPage(pageName, activeRoute = pageName) {
  closeCreativeMenus();
  document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.id === `page-${pageName}`));
  document.querySelectorAll(".nav-item[data-route]").forEach((item) => item.classList.toggle("active", item.dataset.route === activeRoute || (item.dataset.route === "tools" && ["assets", "knowledge", "system", "tools"].includes(activeRoute))));
}

function syncUnifiedNavigation(route) {
  if (route.projectId) {
    state.lastProjectId = route.projectId;
    sessionStorage.setItem("mplus.last-project", route.projectId);
  }
  const project = route.projectId
    ? state.formalProjects.find((item) => item.project_id === route.projectId) || null
    : null;
  elements.formalProjectNavigation.hidden = !route.projectId;
  elements.formalBreadcrumb.hidden = !route.projectId;
  elements.formalMobileExit.hidden = !route.projectId;
  elements.formalMobileMenu.hidden = !route.projectId;
  elements.formalMobileMenu.open = false;
  elements.formalMobileContext.textContent = project?.name || (route.projectId ? "当前项目" : "工作区");
  if (!route.projectId) return;
  elements.formalNavProjectName.textContent = project?.name || "当前项目";
  elements.formalNavProjectState.textContent = project?.status === "archived" ? "已归档 · 只读" : "进行中";
  elements.formalBreadcrumbProject.textContent = project?.name || "当前项目";
  elements.formalBreadcrumbProject.href = formalPath("overview", route.projectId);
  elements.formalBreadcrumbPage.textContent = formalPageLabels[route.page] || "项目";
  const activeProjectPage = route.aux === "materials" ? "materials" : route.navPage || route.page;
  elements.formalProjectNavigation.querySelectorAll("[data-route]").forEach((link) => {
    link.classList.toggle("active", link.dataset.route === activeProjectPage);
    if (link.dataset.route === activeProjectPage) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    if (!['projects', 'new'].includes(link.dataset.route)) {
      link.href = formalPath(link.dataset.route, route.projectId);
    }
  });
  elements.formalMobileMenu.querySelectorAll("[data-route]").forEach((link) => {
    link.href = formalPath(link.dataset.route, route.projectId);
    link.classList.toggle("active", link.dataset.route === activeProjectPage);
  });
  document.querySelectorAll('main a[data-route]').forEach((link) => {
    if (formalSections.has(link.dataset.route)) {
      link.href = formalPath(link.dataset.route, route.projectId);
    }
  });
  elements.formalPlanToScript.href = formalPath("script", route.projectId);
  elements.formalMobileExit.href = "/projects";
  if (elements.formalScriptVersionGovernance) {
    elements.formalScriptVersionGovernance.href = route.projectId
      ? formalPath("script", route.projectId, { view: "versions" })
      : "/projects";
    elements.formalScriptVersionGovernance.setAttribute("aria-disabled", route.projectId ? "false" : "true");
  }
}

async function prepareUnifiedWorkbench(projectId = "") {
  state.agentProjects = state.formalProjects.slice();
  const params = new URLSearchParams(window.location.search);
  const explicit = projectId || params.get("project_id") || "";
  const requested = explicit || state.lastProjectId;
  const project = state.agentProjects.find(item => item.project_id === requested);
  const selected = project?.project_id || "";
  const page = formalRoute().page;
  if (state.workbenchProjectId !== selected || state.workbenchPage !== page) {
    state.workbenchEpoch += 1;
    state.workbenchProjectId = selected;
    state.workbenchPage = page;
    state.workbenchSessionId = "";
    state.assetContext = null;
    elements.assetSearch.disabled = true;
    state.strategyContext = null;
    state.knowledgeContext = null;
    state.versionTree = null;
    state.frozenAssets = [];
    cancelAssetConfirmation(); cancelGovernanceConfirmation(); cancelVersionConfirmation();
    elements.assetLocalPath.value = ""; elements.assetUploadFile.value = "";
    elements.knowledgeStatement.value = "";
    elements.knowledgeEvidence.value = "";
    elements.assetSearchResults.innerHTML = "";
    elements.assetImports.innerHTML = '<div class="workbench-empty">选择项目后读取资产记录。</div>';
    elements.versionTree.innerHTML = '<div class="workbench-empty">选择项目后查看已保存版本。</div>';
    elements.versionPreview.innerHTML = "";
    elements.versionDiff.innerHTML = "";
    elements.strategyGrid.innerHTML = '<div class="workbench-empty">选择项目后读取创作策略。</div>';
    elements.knowledgeRules.innerHTML = "";
    elements.pendingKnowledge.innerHTML = "";
    renderFrozenAssets();
  }
  for (const select of [elements.agentProjectSelect, elements.versionProjectSelect, elements.assetProjectSelect, elements.governanceProjectSelect, document.querySelector("#tools-project-select")]) {
    if (select) populateWorkbenchProjects(select, selected);
  }
  const banner = document.querySelector("#workbench-context");
  banner.hidden = false;
  document.querySelector("#workbench-context-name").textContent = project?.name || "未选择项目";
  document.querySelector("#workbench-context-status").textContent = requested && !project
    ? "指定项目不存在或不可访问，请明确选择可访问的项目。" : selected ? "此处操作使用当前项目。可在项目选择框中切换。" : "请先选择项目，再使用模板、素材或规则。";
  document.querySelector("#tools-project-control").hidden = page !== "tools";
  const returnLink = document.querySelector("#workbench-return");
  const suppliedReturn = params.get("return_to") || "";
  const safeReturn = selected && suppliedReturn.startsWith(`/projects/${encodeURIComponent(selected)}/`) && !suppliedReturn.includes("..") ? suppliedReturn : "";
  returnLink.href = safeReturn || `/tools${selected ? `?project_id=${encodeURIComponent(selected)}` : ""}`;
  returnLink.textContent = safeReturn ? "返回项目" : "返回工具与资源";
  returnLink.hidden = page === "tools";
  document.querySelectorAll("[data-help-route]").forEach(link => {
    link.href = selected ? formalPath(link.dataset.helpRoute, selected) : "/projects";
    link.title = selected ? "" : "先选择一个项目";
  });
  document.querySelectorAll("[data-tool-link]").forEach(link => {
    const kind = link.dataset.toolLink;
    link.href = kind === "templates" ? (selected ? formalPath("brief", selected, {panel: "templates"}) : "/tools")
      : kind === "assistant" ? (selected ? formalPath("assistant", selected) : "/assistant")
      : `/${kind}${selected ? `?project_id=${encodeURIComponent(selected)}` : ""}`;
    link.setAttribute("aria-disabled", ["templates", "assistant"].includes(kind) && !selected ? "true" : "false");
  });
  if (selected) {
    state.lastProjectId = selected;
    sessionStorage.setItem("mplus.last-project", selected);
    syncUnifiedNavigation({...formalRoute(), projectId: selected});
  }
  return selected;
}

async function renderFormalRoute() {
  state.formalFirstDraftEpoch += 1;
  const canonical = formalCanonicalPath(`${window.location.pathname}${window.location.search}${window.location.hash}`);
  if (canonical !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.replaceState({}, "", canonical);
  const route = formalRoute();
  if (firstCreativePicker && firstCreativePicker.location !== `${window.location.pathname}${window.location.search}`) firstPickerClose();
  document.querySelector("#workbench-context").hidden = !["tools", "assets", "knowledge", "versions"].includes(route.page);
  if (state.workbenchPage && route.page !== state.workbenchPage) state.workbenchEpoch += 1;
  syncFormalBriefProject();
  syncFormalTemplateRoute();
  if (!["script", "review"].includes(route.page)) releaseFormalImagePreviews();
  if (route.page !== "script") {
    state.formalScriptEpoch += 1;
    if (state.formalCreative?.loadPromise) {
      state.formalCreative.loaded = false;
      state.formalCreative.loadPromise = null;
    }
  }
  if (!["assistant", "script"].includes(route.page)) { state.agentEpoch += 1; if (state.agentSyncTimer) clearTimeout(state.agentSyncTimer); }
  syncUnifiedNavigation(route);
  const canCreateProject = state.formalBriefAvailable && Boolean(state.agentAuth && state.agentWorkspaceId);
  elements.projectsTitle.textContent = route.create ? "新建项目" : "项目";
  elements.projectsCopy.textContent = route.create ? "先填写名称，再进入需求工作区分析与确认。" : "创建项目，或继续最近的工作。";
  elements.formalProjectCreate.hidden = !route.create || !canCreateProject;
  elements.formalProjectNew.hidden = route.create;
  elements.formalProjectToolbar.hidden = route.create;
  elements.formalProjectDashboard.hidden = route.create;
  const displayPage = route.displayPage || route.page;
  const activeRoute = route.navPage || route.page;
  showFormalPage(displayPage === "assistant" ? "agent" : displayPage, activeRoute);
  if (route.page === "projects") { await loadFormalProjects(); if (!route.create) await loadHomeTasks(); }
  if (route.page === "overview") await loadFormalOverview(route.projectId);
  if (route.page === "brief") await loadFormalBrief(route.projectId);
  if (route.page === "script") await loadCreativeWorkspace(route);
  if (route.page === "materials") { document.querySelector("#page-materials").hidden = false; await loadFormalImages(route.projectId); }
  await loadWorkbenchHandoff();
  syncWorkbenchAssistant();
  renderWorkbenchStructure();
  if (route.page === "script") void maybeRecommendWorkbenchGeneration();
  if (route.page === "versions") {
    const selected = await prepareUnifiedWorkbench(route.projectId);
    if (selected) {
      elements.versionProjectSelect.value = selected;
      await loadVersionWorkbench();
    }
  }
  if (route.page === "review") await loadFormalReview(route.projectId);
  if (route.page === "assistant") {
    state.agentProjects = state.formalProjects.slice();
    populateWorkbenchProjects(elements.agentProjectSelect);
    const requested = route.projectId || new URLSearchParams(window.location.search).get("project_id") || state.lastProjectId;
    const selected = state.agentProjects.some(item => item.project_id === requested) ? requested : "";
    elements.agentProjectSelect.disabled = !state.agentProjects.length;
    elements.agentRefresh.disabled = !selected;
    if (selected) {
      elements.agentProjectSelect.value = selected;
      await selectAgentProject(selected, null);
    } else {
      state.agentEpoch += 1; state.agentSessionId = ""; state.agentProjectId = "";
      elements.agentMessage.value = ""; elements.agentMessageSubmit.disabled = true;
      elements.agentSessionStatus.textContent = "未选择有效项目";
      elements.agentProjectSelect.value = "";
      elements.agentFeed.innerHTML = '<article class="agent-message system"><h3>请选择项目</h3><p>明确选择可访问的项目后，助手才会准备对应会话。</p></article>';
    }
  }
  if (route.page === "tasks") await loadWorkspaceTasks();
  if (route.page === "tools") await prepareUnifiedWorkbench();
  if (route.page === "assets") {
    await prepareUnifiedWorkbench();
    await loadAssetWorkbench();
  }
  if (route.page === "knowledge") {
    await prepareUnifiedWorkbench();
    await loadGovernanceWorkbench();
  }
  if (route.page === "system") await loadUnifiedSystem();
  if (route.page === "admin") await loadFormalAdmin();
  applyFormalArchiveState(route.projectId ? formalProject() : null);
}

async function navigateFormal(path) {
  captureCreativeInputs();
  captureFormalImageInputs();
  if (state.agentProjectId && elements.agentMessage) state.agentInputDrafts.set(state.agentProjectId, elements.agentMessage.value);
  let destination = formalCanonicalPath(path);
  const nextUrl = new URL(destination, window.location.origin);
  const projectMatch = nextUrl.pathname.match(/^\/projects\/([^/]+)\//);
  const nextProject = projectMatch ? decodeURIComponent(projectMatch[1]) : "";
  const current = formalRoute();
  const initial = current.projectId === nextProject && /\/script$/.test(nextUrl.pathname)
    && nextUrl.searchParams.get('panel') === 'structure' ? currentInitialDraft(nextProject) : null;
  if (initial) {
    const originHref = window.location.href;
    if (firstCreativePending(initial)) { showToast('当前初稿仍在处理，请完成后再进入结构。'); return false; }
    if (!await saveFirstCreative(initial)) { showToast('初稿修改尚未保存，已留在本页。请处理保存提示后继续。'); return false; }
    if (window.location.href !== originHref || initial.auth !== state.agentAuth || initial.workspaceId !== state.agentWorkspaceId) return false;
    nextUrl.searchParams.set('creative', '1');
    destination = nextUrl.pathname + nextUrl.search;
  }
  if (current.page === "script" && nextProject !== current.projectId && (formalScriptHasPendingWrites() || creativeProgressPending(creativeProgressQueue()))) {
    const saved = await flushCreativeProgress();
    if (!saved) {
      showToast("当前输入尚未暂存，已留在本页。请重试或放弃修改。");
      return false;
    }
  }
  if (current.page !== "script" && /\/script$/.test(nextUrl.pathname) && current.projectId === nextProject) {
    creativeState().loaded = false;
    creativeState().imageKey = "";
  }
  window.history.pushState({}, "", destination);
  syncFormalBriefProject();
  syncFormalTemplateRoute();
  if (current.projectId && current.projectId === nextProject) await renderFormalRoute();
  else await bootstrapFormal().then(() => renderFormalRoute());
  return true;
}

async function loadFormalOverview(projectId) {
  const epoch = state.formalOverviewEpoch = (state.formalOverviewEpoch || 0) + 1;
  const active = () => state.formalOverviewEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "overview";
  if (!projectId) {
    elements.formalOverviewState.textContent = "请先从项目列表进入项目。";
    elements.formalProjectLifecycle.hidden = true;
    return;
  }
  elements.formalOverviewState.innerHTML = '<p role="status">正在读取项目进度…</p>';
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    const overview = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/overview`);
    if (!active()) return;
    const archived = project.status === "archived";
    const stages = (overview.stages || []).map((stage) => `<div><span>${escapeHtml(stage.label)}</span><strong>${escapeHtml(stage.state)}</strong>${stage.detail ? `<small>${escapeHtml(stage.detail)}</small>` : ""}</div>`).join("");
    const warnings = (overview.warnings || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    const warningHtml = warnings ? `<aside class="ui00-overview-warnings" role="status"><strong>图片注意项</strong><ul>${warnings}</ul></aside>` : "";
    const nextAction = overview.next_action?.href && overview.next_action?.label
      ? `<a class="primary-button compact" href="${escapeHtml(overview.next_action.href)}">${escapeHtml(overview.next_action.label)}</a>`
      : "";
    elements.formalOverviewState.innerHTML = `<strong>${escapeHtml(project.name || "当前项目")}</strong><p>${archived ? "项目已归档。内容仍可查看；恢复后才能继续修改。" : "查看项目进度、阻断原因和下一步。"}</p><p>${escapeHtml(overview.next_action?.reason || "进入项目核对下一步")}</p><div class="ui00-inline-actions">${nextAction}</div>${warningHtml}<details class="project-stage-details"><summary>查看全部制作阶段</summary><div class="ui00-overview-stages">${stages}</div></details>`;
    await loadProjectLifecycle(project);
  } catch (error) { if (active()) elements.formalOverviewState.innerHTML = `<p role="alert">项目进度读取失败：${escapeHtml(formalError(error))}</p><button class="secondary-button compact" data-overview-retry>重新读取</button>`; }
}

const archivedWriteSelectors = [
  "#formal-brief-upload :is(input, textarea, button)", "#formal-brief-confirm",
  "#formal-template-picker :is(input, select, button)",
  "#formal-plan-create", "#formal-plan-confirm-check", "#formal-plan-confirm",
  "#formal-image-synchronize", "#formal-image-understand", "#formal-image-upload-form :is(input, button)", "#formal-image-grid :is(input, textarea, select, button)",
  "#formal-image-version-form :is(input, select, button)", "#formal-review-start",
  "#formal-review-final-form :is(input, textarea, button)",
  "#formal-script-table :is(input, textarea, select)", "#formal-script-instruction-form :is(input, textarea, button)",
  "#formal-script-undo", "#formal-script-redo", "#formal-script-save-form :is(input, textarea, button)",
].join(",");

function applyFormalArchiveState(project) {
  const archived = project?.status === "archived";
  elements.formalArchiveBanner.hidden = !archived;
  if (archived) {
    elements.formalArchiveManage.href = formalPath("overview", project.project_id);
    elements.formalArchiveBannerCopy.textContent = "项目已归档。内容仍可查看；恢复后才能继续修改。";
  }
  document.querySelectorAll(archivedWriteSelectors).forEach((control) => {
    if (archived && !control.disabled) {
      control.disabled = true;
      control.dataset.archivedDisabled = "true";
    } else if (!archived && control.dataset.archivedDisabled === "true") {
      control.disabled = false;
      delete control.dataset.archivedDisabled;
    }
  });
}

async function loadProjectLifecycle(project) {
  const active = () => formalRoute().projectId === project.project_id && formalRoute().page === "overview";
  if (!active()) return;
  state.pendingProjectLifecycle = null;
  elements.formalLifecycleConfirm.hidden = true;
  elements.formalLifecycleConfirmCheck.checked = false;
  elements.formalLifecycleExecute.disabled = true;
  elements.formalProjectLifecycle.hidden = false;
  try {
    const model = await api(`/api/formal/projects/${encodeURIComponent(project.project_id)}/lifecycle`);
    if (!active()) return;
    const archived = model.status === "archived";
    const restore = model.restore || {};
    elements.formalProjectLifecycle.open = archived;
    elements.formalLifecycleState.textContent = archived ? "已归档" : "进行中";
    elements.formalLifecyclePrepare.dataset.lifecycleAction = archived ? "restore" : "archive";
    elements.formalLifecyclePrepare.textContent = archived ? "准备恢复" : "准备归档";
    elements.formalLifecycleReason.placeholder = archived ? "例如：项目重新启动，需要继续编辑" : "例如：项目已完成，暂时退出进行中列表";
    if (archived) {
      elements.formalLifecycleCopy.textContent = restore.available
        ? `恢复窗口剩余 ${restore.remaining_days} 天。恢复只重新开放业务写入，不改动已有资料。`
        : "30 天恢复窗口已结束。项目继续保留且可读取；永久清理尚未开放。";
      elements.formalLifecyclePrepare.disabled = !restore.available;
    } else {
      elements.formalLifecycleCopy.textContent = "归档会将项目移出进行中列表并进入只读状态，30 天内可恢复。不会删除任何业务资料。";
      elements.formalLifecyclePrepare.disabled = false;
    }
  } catch (error) {
    if (!active()) return;
    elements.formalLifecycleState.textContent = "不可操作";
    elements.formalLifecycleCopy.textContent = formalError(error);
    elements.formalLifecyclePrepare.disabled = true;
  }
}

async function prepareProjectLifecycle(event) {
  event.preventDefault();
  if (!elements.formalLifecycleForm.reportValidity()) return;
  const projectId = formalRoute().projectId;
  const action = elements.formalLifecyclePrepare.dataset.lifecycleAction;
  const epoch = state.formalOverviewEpoch;
  const reason = elements.formalLifecycleReason.value.trim();
  const active = () => formalRoute().projectId === projectId && formalRoute().page === "overview" && state.formalOverviewEpoch === epoch;
  if (!projectId || !["archive", "restore"].includes(action)) return;
  if (!reason) {
    showToast(action === "archive" ? "请填写归档原因，不能只填空格。" : "请填写恢复原因，不能只填空格。");
    elements.formalLifecycleReason.focus({ preventScroll: true });
    return;
  }
  elements.formalLifecyclePrepare.disabled = true;
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    if (!active()) return;
    const parameters = {
      expected_project_version: project.project_version,
      idempotency_key: formalRequestKey(`project-${action}`),
      reason,
    };
    const prepared = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/lifecycle/${action}/prepare`, { method: "POST", body: JSON.stringify(parameters) });
    if (!active() || elements.formalLifecycleReason.value.trim() !== reason) return;
    state.pendingProjectLifecycle = { action, projectId, parameters, epoch, commandToken: prepared.command_token };
    elements.formalLifecycleConfirmTitle.textContent = action === "archive" ? "确认归档这个项目" : "确认恢复这个项目";
    elements.formalLifecycleWillChange.textContent = `会修改：${prepared.will_change}`;
    elements.formalLifecycleWillNotChange.textContent = `不会修改：${prepared.will_not_change}`;
    elements.formalLifecycleConfirmLabel.textContent = prepared.confirmation_label || "我已核对并确认";
    elements.formalLifecycleConfirm.hidden = false;
    elements.formalLifecycleConfirm.focus({ preventScroll: true });
  } catch (error) {
    if (!active()) return;
    showToast(formalError(error));
    elements.formalLifecyclePrepare.disabled = false;
  }
}

async function executeProjectLifecycle() {
  const pending = state.pendingProjectLifecycle;
  const active = () => pending && formalRoute().projectId === pending.projectId && formalRoute().page === "overview" && state.formalOverviewEpoch === pending.epoch;
  if (!active() || !elements.formalLifecycleConfirmCheck.checked) return;
  elements.formalLifecycleExecute.disabled = true;
  try {
    await api(`/api/formal/projects/${encodeURIComponent(pending.projectId)}/lifecycle/${pending.action}/execute`, {
      method: "POST", body: JSON.stringify({ ...pending.parameters, command_token: pending.commandToken }),
    });
    if (!active()) return;
    state.pendingProjectLifecycle = null;
    elements.formalLifecycleReason.value = "";
    await loadFormalProjects();
    if (!active()) return;
    await loadFormalOverview(pending.projectId);
    applyFormalArchiveState(formalProject());
    syncUnifiedNavigation(formalRoute());
    showToast(pending.action === "archive" ? "项目已归档，业务资料保持只读。" : "项目已恢复，可以继续业务操作。");
  } catch (error) { if (active()) showToast(formalError(error)); }
}

function cancelProjectLifecycle() {
  state.pendingProjectLifecycle = null;
  elements.formalLifecycleConfirm.hidden = true;
  elements.formalLifecycleConfirmCheck.checked = false;
  elements.formalLifecycleExecute.disabled = true;
  elements.formalLifecyclePrepare.disabled = false;
}

function releaseFormalImagePreviews() {
  state.formalImageEpoch += 1;
  formalPreviewQueue?.clear();
  state.formalImagePreviewUrls.clear();
  state.formalImageThumbnailUrls.clear();
}

function formalImageCollectionPath(projectId, projectVersion) {
  return `/api/formal/projects/${encodeURIComponent(projectId)}/images?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(projectVersion)}`;
}

function formalImagesReady(readiness) {
  if (!readiness || typeof readiness !== "object") return false;
  const usable = Number(readiness.usable_count ?? readiness.pass ?? 0);
  return usable > 0 || Number(readiness.total || 0) === 0;
}

function formalImageStatus(status, evidence = "") {
  if (status === "pass") return evidence === "verified" ? { label: "人工已确认", copy: "分析结果已由人工确认，可用于规划。" } : evidence === "source_reference" ? { label: "来源参考", copy: "已有来源参考分析，可核对后确认。" } : { label: "分析可用", copy: "已有分析结果，人工确认状态单独记录。" };
  return ({
    pass: { label: "已理解", copy: "已有可用的人工图片理解。" },
    pending: { label: "待理解", copy: "请补充图片内容与用途。" },
    low_confidence: { label: "需重新核对", copy: "当前理解信心不足，请人工核对。" },
    blocked: { label: "不可用", copy: "该图片当前不可用于规划。" },
    failed: { label: "理解失败", copy: "请重新核对或补充资料。" },
  })[status] || { label: "待处理", copy: "请核对这张图片。" };
}

function formalBusinessSourceName(source) {
  const raw = String(source?.display_name || source?.business_filename || source?.source_name || source?.label || source?.file || source?.filename || source?.source_file || "").trim();
  if (!raw) return "";
  if (/(?:^|[\\/])(?:media|runtime|packages|artifacts)(?:[\\/])/.test(raw) || /(?:^|[^a-f0-9])[a-f0-9]{32,}(?:[^a-f0-9]|$)/i.test(raw)) return "创意库素材图片";
  return raw.split(/[\\/]/).filter(Boolean).pop() || "素材来源";
}

function formalImageSourceLabel(source) {
  if (!source || typeof source !== "object") return "来源位置暂未提供";
  const sourceName = formalBusinessSourceName(source);
  const packageMaterial = sourceName === "创意库素材图片" && (source.sheet || source.source_sheet) === "media_assets";
  const parts = [
    sourceName,
    packageMaterial && "仅有素材来源",
    source.page && `第 ${source.page} 页`,
    !packageMaterial && source.sheet && `${source.sheet} Sheet`,
    !packageMaterial && source.row && `第 ${source.row} 行`,
    source.slide && `第 ${source.slide} 张`,
    source.image && `图片 ${source.image}`,
  ].filter(Boolean);
  return parts.join(" · ") || "来源位置暂未提供";
}

function formalImageList(value) {
  return Array.isArray(value) ? value.join("、") : "";
}

const formalAnalysisDrafts = new Map();
const formalAnalysisRequests = new Map();

function formalAnalysisRequest(projectId, value = undefined) {
  const key = `mplus.analysis-confirmation:${state.agentWorkspaceId}:${projectId}`;
  if (value !== undefined) {
    if (value === null) formalAnalysisRequests.delete(key);
    else formalAnalysisRequests.set(key, value);
    try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* Memory retains the request when browser storage is unavailable. */ }
  } else if (!formalAnalysisRequests.has(key)) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || "null");
      if (saved?.idempotency_key && Array.isArray(saved.selections) && Number.isInteger(saved.expected_project_version)) formalAnalysisRequests.set(key, saved);
    } catch (_) { /* Ignore invalid browser storage; never infer a completed operation. */ }
  }
  return formalAnalysisRequests.get(key);
}

function formalAnalysisFormValues(form) {
  const data = new FormData(form);
  return { ...Object.fromEntries(data), quality_flags: data.getAll("quality_flags"), negative_flags: data.getAll("negative_flags") };
}

function captureFormalAnalysisForm(form) {
  if (!form?.dataset.analysisDraftKey) return;
  const values = formalAnalysisFormValues(form);
  const key = form.dataset.analysisDraftKey;
  if (JSON.stringify(values) === form.dataset.analysisBaseline) formalAnalysisDrafts.delete(key);
  else formalAnalysisDrafts.set(key, { values, analysisKey: form.dataset.analysisVersion || "" });
}

function captureFormalAnalysisForms() {
  elements.formalImageGrid.querySelectorAll("[data-formal-image-form]").forEach(captureFormalAnalysisForm);
}

function restoreFormalAnalysisForms(images) {
  const projectId = formalRoute().projectId;
  elements.formalImageGrid.querySelectorAll("[data-formal-image-form]").forEach(form => {
    const image = images[Number(form.dataset.formalImageForm)];
    if (!image) return;
    const key = JSON.stringify([projectId, image.image_key]);
    form.dataset.analysisDraftKey = key;
    form.dataset.analysisVersion = image.analysis_key || "";
    form.dataset.analysisBaseline = JSON.stringify(formalAnalysisFormValues(form));
    const draft = formalAnalysisDrafts.get(key);
    if (!draft) return;
    for (const [name, value] of Object.entries(draft.values)) {
      const control = form.elements.namedItem(name);
      if (control?.multiple) [...control.options].forEach(option => { option.selected = (Array.isArray(value) ? value : []).includes(option.value); });
      else if (control) control.value = value;
    }
    form.closest("details").open = true;
    const changed = draft.analysisKey !== (image.analysis_key || "");
    form.dataset.analysisVersion = draft.analysisKey;
    const notice = document.createElement("p");
    notice.dataset.analysisDraftNotice = "1";
    notice.setAttribute("role", "status");
    notice.textContent = changed ? "分析结果已更新，原输入已保留。请对照上方最新分析核对，再保存此处修改。" : "已保留此图片尚未提交的输入。";
    form.prepend(notice);
  });
}

function formalImageForm(index, image) {
  const summary = image.summary || {};
  const status = ["pass", "low_confidence", "blocked", "failed"].includes(image.status) ? image.status : "pass";
  const field = (name, label, value, wide = false, required = true) => `<label class="${wide ? "ui00-image-form-wide" : ""}" for="formal-image-${name}-${index}"><span>${label}</span><input id="formal-image-${name}-${index}" name="${name}" value="${escapeHtml(value || "")}" autocomplete="off"${required ? " required" : ""} /></label>`;
  const flagLabels = {slight_blur:"轻微模糊", severe_blur:"严重模糊", low_resolution:"分辨率不足", overexposed:"过曝", underexposed:"欠曝", bad_crop:"裁切不当", watermark:"有水印", unreadable_subject:"主体不可辨识", external_brand:"外部品牌", forbidden_content:"禁止内容", sensitive_content:"敏感内容"};
  const flags = (name, label) => `<label for="formal-image-${name}-${index}"><span>${label}（可多选或不选）</span><select multiple id="formal-image-${name}-${index}" name="${name}">${(state.formalImages?.analysis_options?.[name] || []).map(value => `<option value="${escapeHtml(value)}"${(summary[name] || []).includes(value) ? " selected" : ""}>${escapeHtml(flagLabels[value] || value)}</option>`).join("")}</select></label>`;
  return `<details class="ui00-image-review"><summary>${image.status === "pass" ? "纠正分析结果" : "手动核对图片"}</summary><form class="ui00-image-form" data-formal-image-form="${index}">
    <label for="formal-image-status-${index}"><span>核对结果</span><select id="formal-image-status-${index}" name="status"><option value="pass"${status === "pass" ? " selected" : ""}>可用</option><option value="low_confidence"${status === "low_confidence" ? " selected" : ""}>需再次核对</option><option value="blocked"${status === "blocked" ? " selected" : ""}>不可用</option><option value="failed"${status === "failed" ? " selected" : ""}>理解失败</option></select></label>
    ${field("caption", "图片描述", summary.caption, true)}
    ${field("tags", "关键词", formalImageList(summary.tags))}
    ${field("subjects", "主体", formalImageList(summary.subjects))}
    ${field("scene", "场景", summary.scene)}
    ${field("purpose", "项目用途", summary.purpose)}
    ${field("quality", "清晰度与质量", summary.quality)}
    ${flags("quality_flags", "质量标记")}
    ${flags("negative_flags", "风险标记")}
    ${field("brand", "品牌信息（无品牌填“无”）", summary.brand)}
    <label class="ui00-image-form-wide" for="formal-image-evidence-${index}"><span>人工核验依据</span><textarea id="formal-image-evidence-${index}" name="evidence" rows="2" autocomplete="off" required placeholder="例：已对照原始资料确认主体、场景与用途…"></textarea></label>
    <p class="ui00-image-form-note">逗号或换行可分隔多个关键词。保存后会记录当前登录人与时间，不会发布或改动脚本。</p>
    <button class="primary-button compact" type="submit">保存人工理解</button>
  </form></details>`;
}

function renderFormalImages(readiness, projectVersion) {
  captureFormalAnalysisForms();
  syncFormalPreviewModel(formalRoute().projectId, projectVersion, readiness);
  state.formalImages = readiness;
  creativeState().planKey = "";
  creativeState().imageKey = "";
  state.formalImageProjectVersion = projectVersion;
  const images = Array.isArray(readiness?.images) ? readiness.images : [];
  const attention = Number(readiness?.attention_count ?? 0) || (
    Number(readiness?.pending || 0) + Number(readiness?.low_confidence || 0)
    + Number(readiness?.blocked || 0) + Number(readiness?.failed || 0)
  );
  const usable = Number(readiness?.usable_count ?? readiness?.pass ?? 0);
  const ready = formalImagesReady(readiness);
  elements.formalImageTotal.textContent = String(Number(readiness?.total || 0));
  elements.formalImagePassed.textContent = String(Number(readiness?.pass || 0));
  elements.formalImagePending.textContent = String(Number(readiness?.pending || 0));
  elements.formalImageAttention.textContent = String(attention);
  const readyWithImages = ready && images.length > 0;
  elements.formalImageState.textContent = usable === Number(readiness?.total || 0) && attention === 0 && usable > 0 ? "全部可用" : usable > 0 ? "部分可用" : ready ? "待选择" : "需要处理";
  elements.formalImageGate.classList.toggle("is-ready", readyWithImages);
  elements.formalImageGateTitle.textContent = usable > 0 ? "可以进入规划" : ready ? "请选择图片处理方式" : "请先处理图片";
  elements.formalImageGateCopy.textContent = usable > 0
    ? attention === 0 && usable === Number(readiness?.total || 0)
      ? `已完成 ${usable} 张图片分析，可以进入规划`
      : `已有 ${usable} 张图片可用于规划；另有 ${attention} 张图片待处理或不可用，不会阻断已通过图片。`
    : ready
      ? Number(readiness?.total || 0) > 0 ? `已有 ${Number(readiness.total)} 张项目图片，当前可用 0 张；请分析或人工核对，也可先生成文字草稿。` : "当前项目没有图片。请选择上传图片、从创意库选图，或先生成文字草稿。"
      : `还有 ${Number(readiness?.pending || 0)} 张待理解，${attention} 张需要关注。当前没有可用于规划的图片。`;
  const missingSources = Number(readiness?.unmaterialized_source_count || 0);
  elements.formalImageSynchronize.hidden = missingSources === 0;
  if (missingSources > 0) {
    elements.formalImageGateCopy.textContent = `已确认资料还有 ${missingSources} 处图片来源尚未接入。点击“接入已确认资料中的图片”后可分析和选用；现有选图保持不变。`;
  }
  elements.formalImageUnderstand.disabled = Boolean(
    state.formalImageAutoBusy || Number(readiness?.pending || 0) < 1
  );
  const routeParams = new URLSearchParams(window.location.search);
  const returningToPlan = routeParams.get('return_to') === 'plan';
  const returningToTextPlan = returningToPlan && routeParams.get("mode") === "text-only";
  elements.formalImageToPlan.href = formalPath("plan", formalRoute().projectId, {
    ...(returningToTextPlan ? { mode: "text-only" } : {}),
    ...(routeParams.get("creative") === "1" ? { creative: "1" } : {}),
  });
  elements.formalImageToPlan.dataset.formalTarget = "1";
  elements.formalImageToPlan.textContent = routeParams.get("return_to") === "plan" ? "返回规划与生成" : "进入规划与生成";
  const returningToShots = routeParams.get("return_to") === "shot-images";
  const returningToCreative = routeParams.get('return_to') === 'creative-images';
  if (returningToCreative) {
    elements.formalImageToPlan.href = formalPath('script', formalRoute().projectId, {panel: 'images', block_id: routeParams.get('block_id') || ''});
    elements.formalImageToPlan.textContent = '返回原段落选图';
  }
  elements.formalImageToPlan.hidden = formalRoute().aux === "materials" && Boolean(state.formalScript?.shots?.length);
  if (returningToShots) {
    elements.formalImageToPlan.href = formalPath("shot-images", formalRoute().projectId, materialsReturnTarget(routeParams));
    elements.formalImageToPlan.textContent = "返回原镜头";
  }
  if (readyWithImages || returningToTextPlan || returningToShots || returningToCreative) {
    elements.formalImageToPlan.removeAttribute("aria-disabled");
    elements.formalImageToPlan.removeAttribute("tabindex");
  } else {
    elements.formalImageToPlan.setAttribute("aria-disabled", "true");
    elements.formalImageToPlan.tabIndex = -1;
  }
  const first = state.formalScript?.state === 'empty' ? firstCreativeView() : null;
  let firstEntry = '';
  if (first?.data?.blocks?.length && returningToCreative && routeParams.get('block_id')) {
    const blocks = first.data.blocks;
    const selectedId = routeParams.get('block_id') || first.activeBlockId || blocks[0].block_id;
    const target = blocks.find(block => block.block_id === selectedId) || blocks[0];
    elements.formalImageGateTitle.textContent = '初稿已可逐段配图';
    elements.formalImageGateCopy.textContent = `已有 ${blocks.length} 段脚本文本。先选目标段落，再从图库选择一张或多张图片；保存时核对对应关系。`;
    elements.formalImageToPlan.removeAttribute('aria-disabled');
    elements.formalImageToPlan.removeAttribute('tabindex');
    elements.formalImageToPlan.href = formalPath('script', first.projectId, {panel: 'images', block_id: target.block_id});
    elements.formalImageToPlan.textContent = '返回初稿配图';
    elements.formalImageToPlan.hidden = false;
    firstEntry = `<section class="ui00-library-entry first-image-entry"><h3>为脚本文本选图</h3><label>目标段落<select id="materials-first-block">${blocks.map(block => `<option value="${escapeHtml(block.block_id)}"${block.block_id === target.block_id ? ' selected' : ''}>第 ${escapeHtml(block.shot_no)} 段 · ${escapeHtml(block.fields['段落'] || '')}</option>`).join('')}</select></label><p id="materials-first-intent">${escapeHtml(target.fields['画面描述'] || '')}</p><button type="button" class="primary-button compact" data-first-open-picker>选图并分配到段落</button><small>支持按本段推荐、浏览创意库、选择项目图片；跨页选择保留。</small></section>`;
    if (!images.length) { elements.formalImageGrid.innerHTML = firstEntry; return; }
  }
  const standaloneLibrary = formalRoute().page === "materials" && !returningToCreative && !returningToShots && !returningToPlan;
  const returnButton = document.querySelector('[data-creative-close-materials]');
  if (returnButton) returnButton.textContent = returningToCreative ? '返回原段落' : returningToShots ? '返回原镜头' : returningToPlan ? '返回规划与生成' : '返回创作';
  if (standaloneLibrary) {
    elements.creativeMaterialsContext.textContent = '项目素材独立管理；选用到段落或镜头时再明确对应关系。';
    elements.formalImageGateTitle.textContent = '图片分析与素材管理';
    elements.formalImageGateCopy.textContent = `项目共 ${images.length} 张图片，${usable} 张可用。上传或从创意库采用后，可返回创作工作台选用。`;
    elements.formalImageToPlan.hidden = true;
  }
  const libraryEntry = firstEntry || `<div class="ui00-library-entry"><div><strong>从创意库补充图片</strong><p>查看同类型参考图，核对后采用。</p></div><button class="secondary-button compact" type="button" data-load-default-images="1">从创意库选图</button><div data-default-image-panel aria-live="polite"></div></div>`;
  renderCreativeAnalysisControls(images);
  if (!images.length) {
    const projectId = formalRoute().projectId;
    elements.formalImageGrid.innerHTML = `<div class="ui00-business-empty"><strong>当前项目没有图片</strong><p>请选择上传项目图片、从同类型脚本的创意库选图，或从创意库采用图片。</p><div class="ui00-inline-actions"><button class="secondary-button compact" type="button" data-upload-project-image="1">上传项目图片</button><button class="primary-button compact" type="button" data-load-default-images="1">从创意库选图</button>${standaloneLibrary ? "" : `<a class="secondary-button compact" href="${formalPath("plan", projectId, { mode: "text-only" })}" data-route="plan" data-text-only-draft="1">仅生成文字草稿</a>`}</div><div data-default-image-panel aria-live="polite"></div></div>`;
    return;
  }
  if (standaloneLibrary) {
    elements.formalImageGateTitle.textContent = "图片分析与素材管理";
    elements.formalImageGateCopy.textContent = `项目共 ${images.length} 张图片，${usable} 张可用。上传或从创意库采用后，可返回创作工作台选用。`;
    elements.formalImageToPlan.hidden = true;
  }
  elements.formalImageGrid.innerHTML = libraryEntry + images.map((image, index) => {
    const status = formalImageStatus(image.status, image.summary?.evidence_status);
    const summary = image.summary || {};
    const facts = image.summary ? `<dl class="ui00-image-facts"><div><dt>图片内容</dt><dd>${escapeHtml(summary.caption || "未提供")}</dd></div><div><dt>场景</dt><dd>${escapeHtml(summary.scene)}</dd></div><div><dt>用途</dt><dd>${escapeHtml(summary.purpose)}</dd></div><div><dt>主体</dt><dd>${escapeHtml(formalImageList(summary.subjects))}</dd></div><div><dt>品牌</dt><dd>${escapeHtml(summary.brand)}</dd></div><div><dt>关键词</dt><dd>${escapeHtml(formalImageList(summary.tags))}</dd></div><div><dt>质量</dt><dd>${escapeHtml(summary.quality)}</dd></div></dl>` : "";
    const sources = (image.sources || []).map((source) => `<li>${escapeHtml(formalImageSourceLabel(source))}</li>`).join("");
    return `<article class="ui00-image-card" data-formal-image-key="${escapeHtml(image.image_key || "")}" data-state="${escapeHtml(image.status || "pending")}" tabindex="-1" aria-label="项目图片 ${index + 1}：${escapeHtml(status.label)}"><div class="ui00-image-preview" id="formal-image-preview-${index}"><span>正在读取图片…</span></div><div class="ui00-image-card-body"><button type="button" class="text-button" data-formal-project-preview-original="${index}">查看原图</button><label class="creative-analysis-choice"><input type="checkbox" data-analysis-select="${escapeHtml(image.image_key)}"${creativeAnalysisEligible(image) ? "" : " disabled"}${creativeState().analysisSelected.has(image.image_key) ? " checked" : ""}>选择此分析结果${creativeAnalysisEligible(image) ? "" : image.summary?.evidence_status === "verified" ? " · 已人工确认" : " · 当前无可核对分析"}</label><header><div><h2>${escapeHtml(shortBusinessText(summary.caption || `项目图片 ${index + 1}`, 36))}</h2><p>${escapeHtml(status.copy)}</p></div><span class="ui00-image-status">${escapeHtml(status.label)}</span></header>${facts ? `<details class="ui00-image-fact-details"><summary>分析详情</summary>${facts}</details>` : ""}<details class="ui00-image-sources"><summary>查看来源</summary><ul>${sources || "<li>来源位置暂未提供</li>"}</ul></details>${formalImageForm(index, image)}</div></article>`;
  }).join("");
  restoreFormalAnalysisForms(images);
}

elements.formalImageGrid.addEventListener('change', event => {
  if (!event.target.matches('#materials-first-block')) return;
  const block = firstCreativeView().data?.blocks?.find(item => item.block_id === event.target.value);
  document.querySelector('#materials-first-intent').textContent = block?.fields['画面描述'] || '';
});
elements.formalImageGrid.addEventListener('click', event => {
  if (event.target.closest('[data-first-open-picker]')) void openFirstCreativeImagePicker(document.querySelector('#materials-first-block').value);
});

function creativeAnalysisEligible(image) {
  return Boolean(image?.analysis_key && image.summary && image.summary.evidence_status !== "verified" && ["pass", "low_confidence"].includes(image.status));
}

function renderCreativeAnalysisControls(images = state.formalImages?.images || []) {
  const current = creativeState();
  let invalidated = 0;
  for (const [key, analysisKey] of current.analysisSelected) {
    if (!images.some(image => image.image_key === key && image.analysis_key === analysisKey && creativeAnalysisEligible(image))) { current.analysisSelected.delete(key); invalidated += 1; }
  }
  if (invalidated) elements.creativeAnalysisResult.textContent = `${invalidated} 张原选择已确认、分析已更新或已不可用，已移出选择；请重新核对。`;
  const selectPending = document.querySelector("[data-analysis-select-visible]");
  if (selectPending) selectPending.textContent = "选择待核对分析";
  elements.creativeAnalysisBatch.hidden = !images.length;
  const selected = current.analysisSelected.size;
  const verified = images.filter(image => image.summary?.evidence_status === "verified").length;
  const pending = images.filter(creativeAnalysisEligible).length;
  elements.creativeAnalysisCount.textContent = `已选 ${selected} 张 · 待核对 ${pending} 张 · 已确认 ${verified} 张`;
  const unresolved = Boolean(formalAnalysisRequest(formalRoute().projectId));
  elements.creativeAnalysisConfirm.textContent = unresolved ? "读取原确认结果 / 重试" : "确认所选分析";
  elements.creativeAnalysisConfirm.disabled = (!selected && !unresolved) || Boolean(current.analysisBusy);
}

async function confirmCreativeImageAnalyses() {
  const projectId = formalRoute().projectId;
  const current = creativeState();
  if (current.analysisBusy || !projectId) return;
  let request = formalAnalysisRequest(projectId);
  if (!request && !current.analysisSelected.size) return;
  const model = state.formalImages;
  const active = () => current === creativeState() && formalRoute().projectId === projectId && model === state.formalImages;
  current.analysisBusy = true;
  renderCreativeAnalysisControls();
  const path = `/api/formal/projects/${encodeURIComponent(projectId)}/images/analysis-confirmations`;
  const query = `?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
  let result;
  let submitting = false;
  try {
    if (request) {
      elements.creativeAnalysisResult.textContent = "正在读取上次确认结果…";
      result = await api(`${path}/${encodeURIComponent(request.idempotency_key)}${query}`);
      if (!active()) return;
      if (result.confirmation_status !== "completed" && result.confirmation_status !== "not_found") throw new Error("原确认结果仍未知，请稍后重新读取。");
    } else {
      request = { expected_project_version: state.formalImageProjectVersion,
        selections: [...current.analysisSelected].map(([image_key, analysis_key]) => ({ image_key, analysis_key })),
        note: "", idempotency_key: formalRequestKey("image-analysis-confirm") };
      formalAnalysisRequest(projectId, request);
    }
    if (result?.confirmation_status !== "completed") {
      elements.creativeAnalysisResult.textContent = "正在确认原批次分析…";
      submitting = true;
      result = await api(`${path}${query}`, { method: "POST", body: JSON.stringify(request) });
    }
    formalAnalysisRequest(projectId, null);
    if (!active()) return;
    for (const selection of request.selections) {
      if (current.analysisSelected.get(selection.image_key) === selection.analysis_key) current.analysisSelected.delete(selection.image_key);
    }
    renderFormalImages(result, result.project_version ?? state.formalImageProjectVersion);
    const updatedModel = state.formalImages;
    await loadFormalImagePreviews(projectId, state.formalImageProjectVersion, state.formalImageEpoch);
    if (current !== creativeState() || formalRoute().projectId !== projectId || updatedModel !== state.formalImages) return;
    elements.creativeAnalysisResult.textContent = `本次新增确认 ${Number(result.newly_confirmed_count || 0)} 张 · 原已确认 ${Number(result.already_confirmed_count || 0)} 张；镜头图片与整片审核分别处理。`;
    current.imageKey = "";
  } catch (error) {
    const rejected = submitting && error.status >= 400 && error.status < 500 && error.status !== 408;
    if (rejected) formalAnalysisRequest(projectId, null);
    if (active()) elements.creativeAnalysisResult.textContent = rejected
      ? `本批次未写入：${formalError(error)}。输入与选择已保留，请核对后重试。`
      : `确认结果暂时未知：${formalError(error)}。原批次已保留，点击“读取原确认结果 / 重试”先核实结果。`;
  } finally { current.analysisBusy = false; if (current === creativeState() && formalRoute().projectId === projectId) renderCreativeAnalysisControls(); }
}

function invalidateFormalDefaultImageProposal() {
  state.formalDefaultImageInputRevision += 1;
  state.formalDefaultImageProposal = null;
  elements.formalImageGrid.querySelector("[data-default-proposal]")?.remove();
}

function formalDefaultCandidateDetails(item) {
  const sources = item.sources?.length ? item.sources : [item.source];
  const locations = sources.length > 1
    ? `<ul>${sources.map(source => `<li>${escapeHtml(planSourceLabel(source))}</li>`).join("")}</ul>`
    : `<p>${escapeHtml(planSourceLabel(sources[0]))}</p>`;
  return `<details><summary>来源与推荐依据</summary><p>${escapeHtml(item.source_status_label || "来源待核对")}</p>${locations}<p>${escapeHtml(item.score_explanation || item.match_basis || "按当前镜头相关性排序，分数不代表识别准确率。")}</p>${formalDefaultTemplateReferences(item)}${formalImageMatchExplanation(item)}</details>`;
}

function formalDefaultGalleryShots(value = state.formalDefaultImages) {
  if (value?.shots?.length) return value.shots;
  if (value?.selection_mode === "category_library_multi") return [];
  return (state.formalImageWorkspace?.shots || state.formalScript?.shots || []).map(shot => {
    const selected = state.formalDefaultImageSelections.get(String(shot.shot_no));
    return { shot_id: shot.shot_id, shot_no: shot.shot_no, visual: shot.visual || "",
      candidates: selected ? [selected] : [], selected_candidate_key: selected?.candidate_key || "" };
  });
}

function formalDefaultAdoptionSelections() {
  if (state.formalDefaultImages?.selection_mode === "category_library_multi") {
    return [...state.formalDefaultLibrarySelected.keys()].map((key, index) => ({shot_no: `library:${index + 1}`, candidate_key: key}));
  }
  const allowed = new Set(creativeAllowedShotIds());
  return formalDefaultGalleryShots()
    .filter(shot => state.formalDefaultImageSelections.has(String(shot.shot_no)) && (!shot.shot_id || allowed.has(shot.shot_id)))
    .map(shot => ({shot_no: shot.shot_no, candidate_key: state.formalDefaultImageSelections.get(String(shot.shot_no)).candidate_key}));
}

function formalDefaultAdoptionReady() {
  if (state.formalDefaultImageRequest !== state.formalDefaultImageLoadedRequest) return false;
  const selections = formalDefaultAdoptionSelections();
  return Boolean(selections.length && selections.every(item => state.formalImagePreviewUrls.has(`default:${item.candidate_key}`)));
}

function updateFormalDefaultPreviewNodes() {
  const panel = elements.formalImageGrid.querySelector("[data-default-image-panel]");
  if (!panel) return;
  panel.querySelectorAll("[data-default-preview]").forEach(target => {
    const url = formalPreviewDisplayUrl(`default:${target.dataset.defaultPreview}`);
    if (!url) return;
    const image = document.createElement("img");
    image.src = url; image.alt = target.dataset.previewCaption || "创意库图片预览";image.width = 1280;image.height = 720;
    target.replaceChildren(image);
  });
  panel.querySelectorAll("[data-default-library-candidate], [data-use-library-candidate]").forEach(input => {
    const key = input.dataset.defaultLibraryCandidate || input.dataset.useLibraryCandidate;
    input.disabled = !formalPreviewDisplayUrl(`default:${key}`) || (Boolean(input.dataset.useLibraryCandidate) && !state.formalDefaultImageShotNo);
  });
  const bulk = panel.querySelector("[data-select-recommended-images]");
  if (bulk) bulk.disabled = state.formalDefaultImageBusy || state.formalDefaultImageRequest !== state.formalDefaultImageLoadedRequest;
  const action = panel.querySelector("[data-propose-default-images]");
  if (action) action.disabled = state.formalDefaultImageBusy || !formalDefaultAdoptionReady();
  const selections = formalDefaultAdoptionSelections();
  const summary = panel.querySelector("[data-default-selection-summary]");
  if (summary) summary.textContent = state.formalDefaultImages?.selection_mode === "category_library_multi"
    ? `已选 ${selections.length} 张，核对后采用。`
    : `已为 ${selections.length} 镜选图。先采用已选图片，其他镜头稍后补充。`;
  const waiting = panel.querySelector("[data-default-preview-pending]");
  if (waiting) waiting.hidden = !selections.length || formalDefaultAdoptionReady();
}

function renderFormalDefaultImages() {
  const panel = elements.formalImageGrid.querySelector("[data-default-image-panel]"), value = state.formalDefaultImages;
  if (!panel || !value) return;
  const shots = formalDefaultGalleryShots(value), multi = value.selection_mode === "category_library_multi";
  const library = state.formalDefaultImageView === "library" || multi;
  const scopeControl = multi ? "" : `<label class="creative-library-scope"><span>本次配图范围</span><select data-library-scope><option value="current"${creativeState().imageMode === "current" ? " selected" : ""}>当前镜头</option><option value="selected"${creativeState().imageMode === "selected" ? " selected" : ""}>已选镜头</option><option value="all"${creativeState().imageMode === "all" ? " selected" : ""}>整份当前稿</option></select></label>`;
  const tabs = `<div class="ui00-library-tabs" role="group" aria-label="选图方式"><button class="${library ? "primary" : "secondary"}-button compact" type="button" data-library-view="library" aria-pressed="${library}">浏览图库</button><button class="${library ? "secondary" : "primary"}-button compact" type="button" data-library-view="recommended" aria-pressed="${!library}">智能推荐</button></div>`;
  const selections = formalDefaultAdoptionSelections();
  const ready = formalDefaultAdoptionReady();
  let content;
  if (library) {
    const candidates = value.library_candidates || [];
    const renderCandidate = item => {
      const url = formalPreviewDisplayUrl(`default:${item.candidate_key}`), selected = multi ? state.formalDefaultLibrarySelected.has(item.candidate_key) : state.formalDefaultImageSelections.get(state.formalDefaultImageShotNo)?.candidate_key === item.candidate_key;
      const action = multi ? `<label class="ui00-library-choice"><input type="checkbox" data-default-library-candidate="${escapeHtml(item.candidate_key)}"${url ? "" : " disabled"}${selected ? " checked" : ""}>选择这张</label>` : `<button class="${selected ? "secondary" : "primary"}-button compact" type="button" data-use-library-candidate="${escapeHtml(item.candidate_key)}"${url && state.formalDefaultImageShotNo ? "" : " disabled"}>${selected ? "已选用" : "用于当前镜头"}</button>`;
      return `<article class="ui00-library-image-card"><div class="ui00-image-shot-preview" data-default-preview="${escapeHtml(item.candidate_key)}" data-preview-caption="${escapeHtml(item.caption)}">${url ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(item.caption)}" width="1280" height="720">` : "<span>预览读取中或不可用</span>"}</div><strong>${escapeHtml(item.caption)}</strong>${action}<button type="button" class="text-button" data-default-preview-original="${escapeHtml(item.candidate_key)}">查看原图${item.is_animated ? " · 动图" : ""}</button>${formalDefaultCandidateDetails(item)}${item.brand_warning ? `<p>${escapeHtml(item.brand_warning)}</p>` : ""}</article>`;
    };
    const groups = candidates.filter(item => !item.duplicate_of).map(item => {
      const copies = candidates.filter(copy => copy.duplicate_of === item.candidate_key);
      return `<div class="ui00-library-candidate-group">${renderCandidate(item)}${copies.length ? `<details class="ui00-duplicate-sources" data-default-duplicate-group${copies.some(copy => state.formalDefaultLibrarySelected.has(copy.candidate_key) || state.formalDefaultImageSelections.get(state.formalDefaultImageShotNo)?.candidate_key === copy.candidate_key) ? " open" : ""}><summary>同图 ${copies.length + 1} 个来源</summary>${copies.map(renderCandidate).join("")}</details>` : ""}</div>`;
    });
    const target = shots.find(shot => String(shot.shot_no) === state.formalDefaultImageShotNo);
    const targetControl = multi ? "" : `<label class="ui00-library-target"><span>当前镜头</span><select data-library-shot><option value="">选择镜头</option>${shots.map(shot => `<option value="${escapeHtml(shot.shot_no)}"${String(shot.shot_no) === state.formalDefaultImageShotNo ? " selected" : ""}>镜头 ${escapeHtml(shot.shot_no)} · ${escapeHtml(shot.visual || "")}</option>`).join("")}</select></label>${target ? `<p class="ui00-library-current-shot">${escapeHtml(target.visual || "")}</p>` : ""}`;
    const picked = multi ? [...state.formalDefaultLibrarySelected.values()] : [...state.formalDefaultImageSelections.values()];
    const pickedSummary = picked.length ? `<details class="ui00-library-picked"><summary>已选 ${picked.length} ${multi ? "张" : "镜"} · 翻页后保留</summary>${multi ? picked.map(item => `<p>${escapeHtml(item.caption)} <button class="text-button" type="button" data-remove-library-candidate="${escapeHtml(item.candidate_key)}">移除</button></p>`).join("") : [...state.formalDefaultImageSelections].map(([shotNo, item]) => `<p>镜头 ${escapeHtml(shotNo)}：${escapeHtml(item.caption)} <button class="text-button" type="button" data-remove-library-shot="${escapeHtml(shotNo)}">取消本镜选择</button></p>`).join("")}</details>` : "";
    content = `${targetControl}<p class="ui00-library-count">${escapeHtml(creativeLibraryCountText(value))}</p><p class="creative-library-selection-count">已选 ${picked.length} ${multi ? "张" : "镜"} · 当前页外 ${picked.filter(item => !candidates.some(candidate => candidate.candidate_key === item.candidate_key)).length} ${multi ? "张" : "镜"}</p>${value.status_counts ? `<details class="creative-library-status"><summary>库存状态</summary><p>待分析 ${Number(value.status_counts.pending_analysis || 0)} · 分析失败 ${Number(value.status_counts.failed_analysis || 0)} · 禁止使用 ${Number(value.status_counts.forbidden || 0)} · 无脚本行 ${Number(value.status_counts.without_script_row || 0)} · 身份待核验 ${Number(value.status_counts.identity_failed || 0)}</p><small>状态可能重叠，不相加作为总量。</small></details>` : ""}${pickedSummary}<div class="ui00-library-grid">${groups.join("") || '<p class="workbench-empty">没有找到图片，换个关键词试试。</p>'}</div><div class="ui00-library-pagination"><button class="secondary-button compact" type="button" data-library-page="${Math.max(1, Number(value.page || 1) - 1)}"${Number(value.page || 1) <= 1 ? " disabled" : ""}>上一页</button><span>第 ${Number(value.page || 1)} 页</span><button class="secondary-button compact" type="button" data-library-page="${Number(value.page || 1) + 1}"${value.has_more ? "" : " disabled"}>下一页</button></div>`;
  } else {
    const scopedShots = shots.filter(shot => !shot.shot_id || creativeAllowedShotIds().includes(shot.shot_id));
    const recommended = shot => (shot.candidates || []).some(item => item.default_eligible !== false);
    const matchedCount = scopedShots.filter(recommended).length;
    const referenceCount = scopedShots.filter(shot => shot.candidates?.length && !recommended(shot)).length;
    content = `<p data-default-match-summary role="status">本次范围 ${scopedShots.length} 镜 · 可推荐 ${matchedCount} 镜 · 仅供参考 ${referenceCount} 镜 · 无候选 ${scopedShots.length - matchedCount - referenceCount} 镜</p><div class="ui00-image-shot-grid">${shots.map((shot, index) => {
      const selected = (shot.candidates || []).find(item => item.candidate_key === shot.selected_candidate_key);
      const url = selected && state.formalImagePreviewUrls.get(`default:${selected.candidate_key}`);
      const referenceReason = shot.candidates?.length && !recommended(shot)
        ? `有参考图片，但暂未满足本镜要求${shot.candidates[0].missing?.length ? "：" + shot.candidates[0].missing.join("、") : ""}。可手动选图核对或补图。` : "";
      return `<article class="ui00-image-shot-card" data-default-image-shot="${escapeHtml(shot.shot_no)}"${shot.shot_id && !creativeAllowedShotIds().includes(shot.shot_id) ? " hidden" : ""}${shot.state === "gap" ? ` data-default-image-gap="${escapeHtml(shot.shot_no)}"` : ""}><header><span>镜头 ${escapeHtml(shot.shot_no)}</span><strong>${escapeHtml(shot.visual)}</strong>${referenceReason ? `<p class="ui00-image-shot-source">${escapeHtml(referenceReason)}</p>` : ""}</header><div class="ui00-image-shot-preview" data-default-preview="${escapeHtml(selected?.candidate_key || "")}" data-preview-caption="镜头 ${escapeHtml(shot.shot_no)} 创意库图片预览">${url ? `<img src="${escapeHtml(url)}" alt="镜头 ${escapeHtml(shot.shot_no)} 创意库图片预览" width="1280" height="720">` : `<span>${shot.candidates?.length ? "请选择图片并查看预览" : "当前镜头没有安全类别候选"}</span>`}</div>${shot.candidates?.length ? `<label><span>推荐图片</span><select data-default-image-select="${index}">${formalDefaultImageOptions(shot)}</select></label>` : `<p>${escapeHtml(shot.gap_reason || "可以先采用其他镜头，再补图或说明暂时缺图。")}</p>`}${shot.candidates?.some(item => item.duplicate_of) ? `<label class="ui00-duplicate-toggle"><input type="checkbox" data-default-image-duplicates="${index}"${shot.show_duplicate_sources ? " checked" : ""}>显示同图的其他来源</label>` : ""}<button class="secondary-button compact" type="button" data-library-for-shot="${escapeHtml(shot.shot_no)}">浏览更多图片</button><div data-default-selection-details>${selected ? formalDefaultCandidateDetails(selected) : ""}</div>${(shot.warnings || []).map(item => `<p>${escapeHtml(item)}</p>`).join("")}</article>`;
    }).join("")}</div>`;
  }
  const warnings = (value.warnings || []).length ? `<details class="ui00-library-warnings"><summary>配图提醒</summary>${value.warnings.map(message => `<p>${escapeHtml(message)}</p>`).join("")}</details>` : "";
  const expandedFilters = Boolean(panel.querySelector(".creative-picker-filter-panel")?.open);
  const filters = creativeLibraryFiltersMarkup(value, state.formalDefaultImageFilters, { prefix: "library",
    matchScope: state.formalDefaultImageMatchScope, hasShot: Boolean(state.formalDefaultImageShotNo), recommended: !library, expanded: expandedFilters });
  const search = `<form class="ui00-library-search" data-library-search-form><label><span class="visually-hidden">搜索图片内容</span><input data-library-search type="search" value="${escapeHtml(state.formalDefaultImageQuery)}" maxlength="300" placeholder="搜索图片内容"></label><button class="secondary-button compact" type="submit">搜索</button><button type="button" class="text-button" data-library-reset>清除筛选</button></form>`;
  panel.innerHTML = `${tabs}${scopeControl}${search}${filters}${warnings}${!library ? '<button class="secondary-button" type="button" data-select-recommended-images>选用可推荐图片</button><p>仅补充本次范围内尚未选用的镜头；保留已保护、已确认和手工选择。缺图镜头可稍后补充。</p>' : ""}${content}<div class="ui00-image-version-actions"><p data-default-selection-summary>${multi ? `已选 ${selections.length} 张，核对后采用。` : `已为 ${selections.length} 镜选图。先采用已选图片，其他镜头稍后补充。`}</p><button class="primary-button" type="button" data-propose-default-images="1"${ready ? "" : " disabled"}>${multi ? "预览采用影响" : "采用可用参考图"}</button><p data-default-preview-pending${!ready && selections.length ? "" : " hidden"}>请等待所选图片预览。</p></div>`;
}

function useFormalLibraryCandidate(key) {
  if (state.formalDefaultImageRequest !== state.formalDefaultImageLoadedRequest) return;
  const value = state.formalDefaultImages, shotNo = state.formalDefaultImageShotNo;
  const candidate = (value?.library_candidates || []).find(item => item.candidate_key === key);
  const shot = formalDefaultGalleryShots(value).find(item => String(item.shot_no) === shotNo);
  if (!candidate || !shot || !formalPreviewDisplayUrl(`default:${key}`)) return;
  invalidateFormalDefaultImageProposal();
  state.formalDefaultImageSelections.set(shotNo, candidate);
  if (!shot.candidates.some(item => item.candidate_key === key)) shot.candidates.push(candidate);
  shot.selected_candidate_key = key;
  renderFormalDefaultImages();
  void loadFormalLibraryPreview(candidate, { original: true });
  showToast(`已用于镜头 ${shotNo}，可继续选图或检查采用影响。`);
}

function toggleFormalLibraryCandidate(key, selected) {
  if (state.formalDefaultImageRequest !== state.formalDefaultImageLoadedRequest) return;
  const candidate = state.formalDefaultImages?.library_candidates?.find(item => item.candidate_key === key);
  if (selected && (!candidate || !formalPreviewDisplayUrl(`default:${key}`))) return;
  invalidateFormalDefaultImageProposal();
  if (selected) state.formalDefaultLibrarySelected.set(key, candidate);
  else state.formalDefaultLibrarySelected.delete(key);
  renderFormalDefaultImages();
  if (selected) void loadFormalLibraryPreview(candidate, { original: true });
}

async function selectFormalRecommendedImages() {
  if (state.formalDefaultImageRequest !== state.formalDefaultImageLoadedRequest || state.formalDefaultImageBusy) return;
  const catalog = state.formalDefaultImages, request = state.formalDefaultImageRequest, projectId = formalRoute().projectId;
  state.formalDefaultImageBusy = true;
  updateFormalDefaultPreviewNodes();
  try {
    // A direct materials link has not loaded the image workspace or saved choices yet.
    if (!state.formalImageWorkspace || state.formalImageProjectId !== projectId)
      await loadFormalShotImages(projectId);
    if (!state.formalImageWorkspace) { showToast("暂时无法核对已有选图，请重试读取后再批量选用。"); return; }
    if (state.formalDefaultImages !== catalog || state.formalDefaultImageRequest !== request
      || formalRoute().projectId !== projectId || formalSurface() !== "materials"
      || state.formalImageWorkspace.project_version !== catalog.project_version) return;
    const allowed = new Set(creativeAllowedShotIds());
    const selections = (catalog.shots || []).flatMap((shot, index) => {
      const script = state.formalScript?.shots?.find(item => item.shot_id === shot.shot_id);
      const existing = state.formalImageWorkspace.shots?.find(item => item.shot_id === shot.shot_id);
      if (!allowed.has(shot.shot_id) || script?.assistant_locked || existing?.confirmed || existing?.explicitly_changed
        || existing?.library_candidate_key || state.formalDefaultImageSelections.has(String(shot.shot_no))) return [];
      const candidate = shot.candidates?.find(item => item.candidate_key === shot.selected_candidate_key && item.default_eligible !== false);
      return candidate ? [{ index, key: candidate.candidate_key }] : [];
    });
    await Promise.all(selections.map(item => selectFormalDefaultCandidate(item.index, item.key)));
  } finally {
    state.formalDefaultImageBusy = false;
    updateFormalDefaultPreviewNodes();
  }
}

async function selectFormalDefaultCandidate(index, key) {
  if (state.formalDefaultImageRequest !== state.formalDefaultImageLoadedRequest) return;
  const shot = state.formalDefaultImages?.shots?.[index];
  const candidate = shot?.candidates?.find(item => item.candidate_key === key);
  if (!candidate) return;
  invalidateFormalDefaultImageProposal();
  const catalog = state.formalDefaultImages, epoch = state.formalImageEpoch, request = state.formalDefaultImageRequest;
  const projectId = formalRoute().projectId, version = state.formalImageProjectVersion;
  const active = () => state.formalDefaultImages === catalog && shot.selected_candidate_key === key
    && state.formalDefaultImageSelections.get(String(shot.shot_no))?.candidate_key === key
    && state.formalDefaultImageRequest === request && state.formalImageProjectVersion === version
    && state.formalImageEpoch === epoch && formalRoute().projectId === projectId && formalSurface() === "materials";
  shot.selected_candidate_key = key;
  state.formalDefaultImageSelections.set(String(shot.shot_no), candidate);
  const select = elements.formalImageGrid.querySelector(`[data-default-image-select="${index}"]`);
  if (select) select.innerHTML = formalDefaultImageOptions(shot);
  const card = select?.closest?.("[data-default-image-shot]");
  const preview = card?.querySelector("[data-default-preview]");
  if (preview) { preview.dataset.defaultPreview = key; preview.innerHTML = "<span>预览读取中…</span>"; }
  const details = card?.querySelector("[data-default-selection-details]");
  if (details) details.innerHTML = formalDefaultCandidateDetails(candidate);
  updateFormalDefaultPreviewNodes();
  try {
    if (!state.formalImagePreviewUrls.has(`default:${key}`)) {
      const url = await requestFormalPreview(formalDefaultPreviewPath(candidate, version), { candidate, model: catalog, owner: `default-selected:${projectId}:${epoch}`, priority: 0 });
      if (!active()) return;
      state.formalImagePreviewUrls.set(`default:${key}`, url);
    }
    if (active()) updateFormalDefaultPreviewNodes();
  } catch (error) {
    if (!active() || state.formalImagePreviewUrls.has(`default:${key}`)) return;
    updateFormalDefaultPreviewNodes();
    if (preview?.isConnected && error?.name !== "AbortError") preview.innerHTML = formalPreviewFailureMarkup(error,
      `data-default-preview-retry="${escapeHtml(key)}" data-preview-original="true"`);
    showToast(formalError(error));
  }
}

function formalDefaultPreviewPath(candidate, version) {
  const url = new URL(candidate.preview_url, window.location.origin);
  url.searchParams.set("workspace_id", state.agentWorkspaceId);
  url.searchParams.set("expected_project_version", version);
  return `${url.pathname}${url.search}`;
}

function formalDefaultTemplateReferences(candidate) {
  const references = candidate?.template_recommendations || [];
  if (!references.length) return "";
  return `<details class="ui00-image-match-explanation"><summary>查看同类型参考镜头</summary><ul>${references.map((item) => `<li>${escapeHtml(`镜头 ${item.shot_no || "待确认"} · ${item.structure || ""} · ${item.shot_function || ""}`)}</li>`).join("")}</ul></details>`;
}

async function loadFormalLibraryPreview(candidate, { original = false, owner = state.formalDefaultImagePreviewOwner } = {}) {
  if (!candidate?.preview_url) return;
  const projectId = formalRoute().projectId, projectVersion = state.formalImageProjectVersion, epoch = state.formalImageEpoch;
  const model = state.formalDefaultImages, key = `default:${candidate.candidate_key}`;
  const active = () => state.formalImageEpoch === epoch && formalRoute().projectId === projectId
    && formalSurface() === "materials" && model?.package?.manifest_sha256 === state.formalDefaultImages?.package?.manifest_sha256;
  try {
    const url = await requestFormalPreview(formalDefaultPreviewPath(candidate, projectVersion), {
      candidate, model, variant: original ? "original" : "thumbnail", priority: original ? 0 : 1,
      owner: original ? `default-selected:${projectId}:${epoch}` : owner,
    });
    if (!active()) return;
    (original ? state.formalImagePreviewUrls : state.formalImageThumbnailUrls).set(key, url);
    updateFormalDefaultPreviewNodes();
  } catch (error) {
    if (!active() || error?.name === "AbortError") return;
    updateFormalDefaultPreviewNodes();
    for (const target of elements.formalImageGrid.querySelectorAll("[data-default-preview]")) {
      if (target.dataset.defaultPreview === candidate.candidate_key) target.innerHTML = formalPreviewFailureMarkup(error,
        `data-default-preview-retry="${escapeHtml(candidate.candidate_key)}" data-preview-original="${original}"`);
    }
  }
}

async function loadFormalDefaultImages(options = {}) {
  const projectId = formalRoute().projectId, projectVersion = state.formalImageProjectVersion, epoch = state.formalImageEpoch;
  const panel = elements.formalImageGrid.querySelector("[data-default-image-panel]");
  if (!projectId || projectVersion === null || !panel || state.formalDefaultImageConfirming) return;
  if (formalScriptHasPendingWrites() && !await flushFormalScriptChanges()) return;
  const context = `${projectId}:${projectVersion}`;
  if (state.formalDefaultImageContext !== context) {
    state.formalDefaultImageContext = context;
    state.formalDefaultImages = null;
    state.formalDefaultImageSelections.clear();state.formalDefaultLibrarySelected.clear();
    state.formalDefaultImageQuery = "";state.formalDefaultImagePage = 1;
    state.formalDefaultImageView = "library";
    state.formalDefaultImageMatchScope = "all"; state.formalDefaultImageFilters = {};
    state.formalDefaultImageShotNo = new URLSearchParams(window.location.search).get("focus_shot") || "";
  }
  state.formalDefaultImageView = options.view ?? state.formalDefaultImageView;
  state.formalDefaultImageQuery = options.query ?? state.formalDefaultImageQuery;
  state.formalDefaultImagePage = options.page ?? state.formalDefaultImagePage;
  state.formalDefaultImageShotNo = options.shotNo ?? state.formalDefaultImageShotNo;
  state.formalDefaultImageMatchScope = options.matchScope ?? state.formalDefaultImageMatchScope;
  state.formalDefaultImageFilters = options.filters ?? state.formalDefaultImageFilters;
  const request = ++state.formalDefaultImageRequest;
  state.formalDefaultImageListController?.abort();
  state.formalDefaultImageListController = typeof AbortController === "function" ? new AbortController() : null;
  formalPreviewQueue?.release(state.formalDefaultImagePreviewOwner);
  state.formalDefaultImagePreviewOwner = `default-page:${projectId}:${epoch}:${request}`;
  invalidateFormalDefaultImageProposal();
  const requestPath = creativePickerCandidatesPath({ projectId, projectVersion, source: state.formalDefaultImageView,
    query: state.formalDefaultImageQuery, page: state.formalDefaultImagePage, shotNo: state.formalDefaultImageShotNo,
    matchScope: state.formalDefaultImageMatchScope, filters: state.formalDefaultImageFilters });
  const active = () => request === state.formalDefaultImageRequest && state.formalImageEpoch === epoch && formalRoute().projectId === projectId && formalSurface() === "materials";
  if (!state.formalDefaultImages) panel.textContent = "正在读取图库…";
  else panel.setAttribute("aria-busy", "true");
  panel.querySelectorAll("[data-default-library-candidate], [data-use-library-candidate], [data-default-image-select], [data-propose-default-images]").forEach(input => { input.disabled = true; });
  try {
    const value = await api(requestPath, { signal: state.formalDefaultImageListController?.signal });
    if (!active()) return;
    syncFormalPreviewModel(projectId, projectVersion, value);
    invalidateFormalDefaultImageProposal();
    state.formalDefaultImageLoadedRequest = request;
    value.shots = formalDefaultGalleryShots(value);
    for (const shot of value.shots || []) {
      const selected = state.formalDefaultImageSelections.get(String(shot.shot_no));
      if (selected) {
        if (!shot.candidates.some(item => item.candidate_key === selected.candidate_key)) shot.candidates.push(selected);
        shot.selected_candidate_key = selected.candidate_key;
      }
    }
    state.formalDefaultImages = value;
    renderFormalDefaultImages();
    const previews = new Map(creativePickerCandidateGroups(value.library_candidates || []).map(group => [group[0].candidate_key, group[0]]));
    for (const shot of value.shots || []) {
      const selected = shot.candidates?.find(item => item.candidate_key === shot.selected_candidate_key);
      if (selected) previews.set(selected.candidate_key, selected);
    }
    const selected = new Map([...state.formalDefaultLibrarySelected.values(), ...state.formalDefaultImageSelections.values()]
      .map(candidate => [candidate.candidate_key, candidate]));
    selected.forEach((candidate, key) => previews.set(key, candidate));
    await Promise.allSettled([...previews.values()].map(candidate => loadFormalLibraryPreview(candidate, {
      original: selected.has(candidate.candidate_key), owner: state.formalDefaultImagePreviewOwner,
    })));
    if (!active()) return;
    updateFormalDefaultPreviewNodes();
  } catch (error) {
    if (active() && error?.name !== "AbortError") panel.innerHTML = `<strong>图库暂不可用</strong><p>${escapeHtml(formalError(error))}</p><button class="secondary-button compact" type="button" data-load-default-images="1">重试</button>`;
  } finally { if (active()) panel.removeAttribute("aria-busy"); }
}

async function proposeFormalDefaultImages() {
  const projectId = formalRoute().projectId;
  const projectVersion = state.formalImageProjectVersion;
  const panel = elements.formalImageGrid.querySelector("[data-default-image-panel]");
  if (creativeImageDraftGate()) return;
  if (projectVersion === null || !state.formalDefaultImages || !panel || state.formalDefaultImageBusy || !formalDefaultAdoptionReady()) return;
  const epoch = state.formalImageEpoch, revision = state.formalDefaultImageInputRevision;
  const button = panel.querySelector("[data-propose-default-images]");
  state.formalDefaultImageBusy = true;
  if (button) { button.disabled = true; button.textContent = "准备预览…"; }
  try {
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/images/default-proposals?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {
      method: "POST",
      body: JSON.stringify({
        expected_project_version: projectVersion,
        ...(state.formalDefaultImages.base_version_id && Number.isInteger(state.formalDefaultImages.script_revision) ? {expected_script_revision: state.formalDefaultImages.script_revision} : {}),
        selections: formalDefaultAdoptionSelections(),
        ...(state.formalDefaultImages.selection_mode !== "category_library_multi" ? { allowed_shot_ids: creativeAllowedShotIds() } : {}),
        idempotency_key: formalRequestKey("default-image-adoption"),
      }),
    });
    if (formalRoute().projectId !== projectId || formalSurface() !== "materials" || state.formalImageEpoch !== epoch || state.formalDefaultImageInputRevision !== revision) return;
    state.formalDefaultImageProposal = result;
    const response = result.response || {};
    panel.insertAdjacentHTML("beforeend", `<div class="ui00-image-version-message" data-default-proposal><strong>${escapeHtml(response.summary || "采用提案已生成")}</strong><p>${escapeHtml(response.will_change || "")}</p>${(response.warnings || []).map((item) => `<p>${escapeHtml(item)}</p>`).join("")}<button class="primary-button compact" type="button" data-confirm-default-images="1">确认采用这些创意库图片</button></div>`);
  } catch (error) {
    if (formalRoute().projectId === projectId && panel.isConnected) panel.insertAdjacentHTML("beforeend", `<div class="ui00-image-version-message"><strong>采用提案失败</strong><p>${escapeHtml(formalError(error))}</p></div>`);
  } finally {
    state.formalDefaultImageBusy = false;
    if (button?.isConnected) { button.disabled = false; button.textContent = "预览采用影响"; }
  }
}

async function confirmFormalDefaultImages() {
  const pending = state.formalDefaultImageProposal;
  const projectId = formalRoute().projectId, epoch = state.formalImageEpoch;
  const focusShot = new URLSearchParams(window.location.search).get("focus_shot") || "";
  const returning = new URLSearchParams(window.location.search).get("return_to") === "shot-images";
  if (creativeImageDraftGate()) return;
  if (!pending?.task_id || !pending?.response?.proposal_digest || state.formalDefaultImageBusy) return;
  state.formalDefaultImageBusy = true;
  state.formalDefaultImageConfirming = true;
  const button = elements.formalImageGrid.querySelector("[data-confirm-default-images]");
  if (button) { button.disabled = true; button.textContent = "采用中…"; }
  try {
    const result = await api(`/api/agent/tasks/${encodeURIComponent(pending.task_id)}/confirm`, { method: "POST", body: JSON.stringify({ proposal_digest: pending.response.proposal_digest }) });
    requireCompletedAgentAction(result);
    if (formalRoute().projectId !== projectId || state.formalImageEpoch !== epoch) return;
    // Preserve current inputs before recording the adopted choice. Retire this stale
    // workspace so navigation cannot overwrite the newer adoption with old selections.
    captureFormalImageInputs();
    rememberFormalDefaultAdoptions(projectId, result.response);
    state.formalImageWorkspace = null;
    state.formalImageDirty = false;
    if (state.formalScript && Number.isInteger(result.response?.project_version)) state.formalScript.project_version = result.response.project_version;
    await refreshCreativeOverview(projectId);
    state.formalDefaultImageProposal = null;
    state.formalDefaultImageSelections.clear();state.formalDefaultLibrarySelected.clear();
    creativeState().imageKey = "";
    if (returning) await changeCreativePanel("images");
    else {
      await loadFormalImages(projectId);
      elements.formalImageUploadState.innerHTML = `<strong>创意库图片已采用</strong><a class="primary-button compact" data-image-return="1" href="${formalPath("shot-images", projectId)}">继续逐镜确认</a>`;
    }
    showToast("图片已采用，请逐镜核对。 ");
  } catch (error) {
    if (formalRoute().projectId === projectId) {
      const panel = elements.formalImageGrid.querySelector("[data-default-image-panel]");
      if (panel) panel.insertAdjacentHTML("beforeend", `<p role="alert">${escapeHtml(formalError(error))}</p>`);
    }
  } finally {
    state.formalDefaultImageConfirming = false;
    state.formalDefaultImageBusy = false;
    if (button?.isConnected) { button.disabled = false; button.textContent = "确认采用这些创意库图片"; }
  }
}

function createFormalPreviewQueue({ load, maxConcurrent = 4, maxBytes = 128 * 1024 * 1024,
  timeoutMs = 10000, createUrl = blob => URL.createObjectURL(blob), revokeUrl = url => URL.revokeObjectURL(url) } = {}) {
  const jobs = new Map(), cache = new Map(), temporary = new Map();
  let active = 0, bytes = 0, sequence = 0, scheduled = false;
  const cancelled = () => Object.assign(new Error("预览请求已取消"), { name: "AbortError" });
  const evict = key => {
    const item = cache.get(key);
    if (!item) return;
    bytes -= item.size; cache.delete(key); revokeUrl(item.url);
  };
  const touch = key => {
    const item = cache.get(key);
    if (item) { cache.delete(key); cache.set(key, item); }
    return item;
  };
  const settle = (job, error, url) => {
    if (jobs.get(job.key) === job) jobs.delete(job.key);
    for (const subscriber of job.subscribers) error ? subscriber.reject(error) : subscriber.resolve(url);
    job.subscribers.clear();
  };
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    Promise.resolve().then(() => { scheduled = false; pump(); });
  };
  const pump = () => {
    const waiting = [...jobs.values()].filter(job => !job.started && !job.cancelled && job.subscribers.size)
      .sort((a, b) => a.priority - b.priority || a.sequence - b.sequence);
    while (active < Math.min(4, Math.max(1, maxConcurrent)) && waiting.length) {
      const job = waiting.shift();
      job.started = true; active += 1;
      job.controller = typeof AbortController === "function" ? new AbortController() : { abort() {}, signal: undefined };
      let timer;
      const timeout = new Promise((_resolve, reject) => {
        timer = setTimeout(() => {
          const error = Object.assign(new Error("图片加载超时，请重试"), { code: "PREVIEW_TIMEOUT" });
          reject(error); job.controller.abort();
        }, timeoutMs);
      });
      let loading;
      try { loading = Promise.resolve(load(job.path, { signal: job.controller.signal })); }
      catch (error) { loading = Promise.reject(error); }
      Promise.race([loading, timeout])
        .then(blob => {
          if (job.cancelled || !job.subscribers.size) return;
          const item = { url: createUrl(blob), size: blob.size };
          if (item.size > maxBytes) temporary.set(job.key, { ...item, owners: new Set([...job.subscribers].map(item => item.owner)) });
          else {
            cache.set(job.key, item); bytes += item.size;
            while (bytes > maxBytes && cache.size) evict(cache.keys().next().value);
          }
          settle(job, null, item.url);
        }).catch(error => settle(job, error))
        .finally(() => {
          clearTimeout(timer); active -= 1;
          if (jobs.get(job.key) === job) jobs.delete(job.key);
          schedule();
        });
    }
  };
  const abandon = job => {
    job.cancelled = true;
    if (jobs.get(job.key) === job) jobs.delete(job.key);
    job.controller?.abort();
  };
  return {
    request({ key, path, owner = "page", priority = 1 }) {
      const cached = touch(key);
      if (cached) return Promise.resolve(cached.url);
      const transient = temporary.get(key);
      if (transient) { transient.owners.add(owner); return Promise.resolve(transient.url); }
      let job = jobs.get(key);
      if (!job) {
        job = { key, path, priority, sequence: sequence++, subscribers: new Set(), started: false, cancelled: false };
        jobs.set(key, job);
      }
      job.priority = Math.min(job.priority, priority);
      const promise = new Promise((resolve, reject) => job.subscribers.add({ owner, resolve, reject }));
      pump();
      return promise;
    },
    release(owner) {
      if (!owner) return;
      for (const [key, item] of temporary) {
        item.owners.delete(owner);
        if (!item.owners.size) { temporary.delete(key); revokeUrl(item.url); }
      }
      for (const job of jobs.values()) {
        for (const subscriber of job.subscribers) {
          if (subscriber.owner !== owner) continue;
          subscriber.reject(cancelled()); job.subscribers.delete(subscriber);
        }
        if (!job.subscribers.size) abandon(job);
      }
      schedule();
    },
    clear() {
      for (const job of jobs.values()) { settle(job, cancelled()); abandon(job); }
      for (const key of [...cache.keys()]) evict(key);
      for (const item of temporary.values()) revokeUrl(item.url);
      temporary.clear();
    },
  };
}

let formalPreviewQueue = null;
let formalPreviewSession = { auth: state.agentAuth, workspace: state.agentWorkspaceId, generation: 0 };
let formalPreviewModel = {};

function forgetFormalPreviewUrl(url) {
  for (const cache of [state.formalImagePreviewUrls, state.formalImageThumbnailUrls, state.agentPreviewUrls]) {
    for (const [key, value] of cache) if (value === url) cache.delete(key);
  }
  URL.revokeObjectURL(url);
}

function clearFormalPreviewSession() {
  formalPreviewQueue?.clear();
  state.formalImagePreviewUrls.clear(); state.formalImageThumbnailUrls.clear(); state.agentPreviewUrls.clear();
  formalPreviewModel = {};
  formalPreviewSession = { auth: state.agentAuth, workspace: state.agentWorkspaceId, generation: formalPreviewSession.generation + 1 };
}

function syncFormalPreviewModel(projectId, projectVersion, model = {}) {
  const context = { projectId, projectVersion };
  if (model?.base_version_id) context.baseVersionId = model.base_version_id;
  if (model?.package?.manifest_sha256) context.packageSha = model.package.manifest_sha256;
  if (Object.entries(context).some(([key, value]) => formalPreviewModel[key] !== undefined && formalPreviewModel[key] !== value)) {
    clearFormalPreviewSession();
  }
  formalPreviewModel = { ...formalPreviewModel, ...context };
}

function formalPreviewCacheKey(path, { candidate = {}, model = {}, variant = "original" } = {}) {
  candidate = candidate || {}; model = model || {};
  if (formalPreviewSession.auth !== state.agentAuth || formalPreviewSession.workspace !== state.agentWorkspaceId) {
    if (formalPreviewQueue) clearFormalPreviewSession();
    else formalPreviewSession = { auth: state.agentAuth, workspace: state.agentWorkspaceId, generation: formalPreviewSession.generation + 1 };
  }
  const url = new URL(path, window.location.origin);
  return JSON.stringify([formalPreviewSession.generation, state.agentWorkspaceId, url.pathname,
    url.searchParams.get("expected_project_version") || model.project_version || "",
    model.base_version_id || "", candidate.image_key || candidate.candidate_key || candidate.media_id || "",
    candidate.content_sha256 || "", model.package?.manifest_sha256 || "", variant]);
}

function formalPreviewVariantPath(path, variant = "original") {
  const url = new URL(path, window.location.origin);
  if (/\/images\/(?:default-candidates\/)?[^/]+\/preview$/.test(url.pathname)) url.searchParams.set("variant", variant);
  return `${url.pathname}${url.search}`;
}

function requestFormalPreview(path, options = {}) {
  const scoped = new URL(path, window.location.origin);
  if (/^\/api\/formal\/projects\/[^/]+\/images\/(?:default-candidates\/)?[^/]+\/preview$/.test(scoped.pathname)) {
    if (!scoped.searchParams.has("workspace_id")) scoped.searchParams.set("workspace_id", state.agentWorkspaceId);
    const version = options.model?.project_version ?? state.formalScript?.project_version;
    if (!scoped.searchParams.has("expected_project_version") && version != null) scoped.searchParams.set("expected_project_version", version);
    const baseVersion = options.model?.base_version_id;
    if (!scoped.searchParams.has("version_id") && baseVersion) scoped.searchParams.set("version_id", baseVersion);
    path = `${scoped.pathname}${scoped.search}`;
  }
  const key = formalPreviewCacheKey(path, options);
  if (!formalPreviewQueue) formalPreviewQueue = createFormalPreviewQueue({
    load: (requestPath, requestOptions) => readFormalImagePreview(requestPath, requestOptions),
    revokeUrl: forgetFormalPreviewUrl,
  });
  return formalPreviewQueue.request({ key, path: formalPreviewVariantPath(path, options.variant),
    owner: options.owner || `formal:${state.formalImageEpoch}`, priority: options.priority ?? 1 });
}

function formalPreviewDisplayUrl(key) {
  return state.formalImagePreviewUrls.get(key) || state.formalImageThumbnailUrls.get(key) || "";
}

function formalPreviewFailureMarkup(error, retryAttribute) {
  const message = error?.code === "PREVIEW_TIMEOUT" ? "图片加载超时" : error?.status === 422 ? "图片读取参数不完整，请刷新后重试" : error?.status === 404 ? "图片来源暂不可用" : error?.status === 409 ? "项目版本已变化，请刷新后重试" : error?.status === 401 || error?.status === 403 ? "当前登录状态无法读取图片" : error?.payload?.detail?.message || "图片暂未加载";
  return `<span class="creative-preview-error" role="status">${escapeHtml(message)}<button type="button" class="secondary-button compact" ${retryAttribute}>重试此图</button></span>`;
}

async function readFormalImagePreview(path, { signal } = {}) {
  const headers = new Headers();
  if (state.agentAuth) headers.set("X-MPlus-Agent-Auth", state.agentAuth);
  const response = await fetch(path, { headers, signal });
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) clearFormalPreviewSession();
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    const error = new Error("preview unavailable");
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  const blob = await response.blob();
  const preview = document.createElement("img");
  if (typeof preview.decode === "function") {
    const url = URL.createObjectURL(blob);
    try { preview.src = url; await preview.decode(); }
    finally { URL.revokeObjectURL(url); }
  }
  return blob;
}

async function loadFormalImagePreviews(projectId, projectVersion, epoch, { originalIndex = null, retryIndex = null } = {}) {
  const images = Array.isArray(state.formalImages?.images) ? state.formalImages.images : [];
  const currentImages = state.formalImages;
  const active = () => state.formalImageEpoch === epoch && state.formalImages === currentImages
    && formalRoute().projectId === projectId && formalSurface() === "materials";
  await Promise.allSettled(images.map(async (image, index) => {
    if (originalIndex !== null && originalIndex !== index || retryIndex !== null && retryIndex !== index) return;
    const target = document.querySelector(`#formal-image-preview-${index}`);
    if (!target) return;
    try {
      const path = `/api/formal/projects/${encodeURIComponent(projectId)}/images/${encodeURIComponent(image.image_key)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(projectVersion)}`;
      const original = originalIndex === index;
      const url = await requestFormalPreview(path, { candidate: image, model: { project_version: projectVersion },
        owner: `materials:${projectId}:${epoch}`, variant: original ? "original" : "thumbnail", priority: original ? 0 : 1 });
      if (!active()) return;
      (original ? state.formalImagePreviewUrls : state.formalImageThumbnailUrls).set(image.image_key, url);
      const preview = document.createElement("img");
      preview.src = formalPreviewDisplayUrl(image.image_key);
      preview.alt = image.summary?.caption ? `项目图片：${image.summary.caption}` : `项目图片 ${index + 1}`;
      preview.width = 1280; preview.height = 720; preview.loading = "lazy"; preview.decoding = "async";
      target.replaceChildren(preview);
    } catch (error) {
      if (active() && target.isConnected && error?.name !== "AbortError") target.innerHTML = formalPreviewFailureMarkup(error,
        `data-formal-project-preview-retry="${index}" data-preview-original="${originalIndex === index}"`);
    }
  }));
}

async function synchronizeFormalImages() {
  const projectId = formalRoute().projectId, epoch = state.formalImageEpoch;
  if (!projectId || elements.formalImageSynchronize.disabled) return;
  elements.formalImageSynchronize.disabled = true;
  try {
    const readiness = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/images/synchronize?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {
      method: "POST", body: JSON.stringify({ expected_project_version: state.formalImageProjectVersion }),
    });
    if (formalRoute().projectId === projectId && state.formalImageEpoch === epoch) {
      renderFormalImages(readiness, state.formalImageProjectVersion);
      await loadFormalImagePreviews(projectId, state.formalImageProjectVersion, epoch);
    }
  } catch (error) {
    if (formalRoute().projectId === projectId && state.formalImageEpoch === epoch) showToast(formalError(error));
  } finally {
    if (formalRoute().projectId === projectId && state.formalImageEpoch === epoch) elements.formalImageSynchronize.disabled = false;
  }
}

async function loadFormalImages(projectId) {
  captureFormalAnalysisForms();
  state.formalImageBusy = false;
  state.formalImageAutoBusy = false;
  const epoch = state.formalImageEpoch;
  state.formalImages = null;
  const available = Boolean(projectId && state.formalBriefAvailable && state.agentAuth && state.agentWorkspaceId);
  elements.formalImageProjectRequired.hidden = available;
  elements.formalImageWorkbench.hidden = !available;
  if (!available) { elements.formalImageState.textContent = "等待项目"; return; }
  elements.formalImageState.textContent = "读取中";
  elements.formalImageUnderstand.disabled = true;
  elements.formalImageGate.classList.remove("is-ready");
  elements.formalImageGateTitle.textContent = "正在读取图片…";
  elements.formalImageGateCopy.textContent = "正在核对当前 Brief 与图片状态。";
  elements.formalImageGrid.innerHTML = '<div class="ui00-business-empty"><strong>正在读取项目图片</strong><p>请稍候。</p></div>';
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    const readiness = await api(formalImageCollectionPath(projectId, project.project_version));
    if (state.formalImageEpoch !== epoch || formalRoute().projectId !== projectId || formalSurface() !== "materials") return;
    renderFormalImages(readiness, project.project_version);
    const params = new URLSearchParams(window.location.search);
    const supplementImageKey = params.get("supplement_image_key") || "";
    if (supplementImageKey) {
      const card = [...elements.formalImageGrid.querySelectorAll("[data-formal-image-key]")]
        .find((item) => item.dataset.formalImageKey === supplementImageKey);
      if (card) {
        const review = card.querySelector("details.ui00-image-review");
        if (review) review.open = card.dataset.state !== "pending" && card.dataset.state !== "pass";
        card.scrollIntoView({ block: "start" });
        card.focus({ preventScroll: true });
        elements.formalImageGateCopy.textContent = "图片已加入。点击“分析图片”，完成后返回原镜头。";
      }
    }
    await loadFormalImagePreviews(projectId, project.project_version, epoch);
    if (state.formalImageEpoch !== epoch || formalRoute().projectId !== projectId || formalSurface() !== "materials") return;
    if (params.get("pick") === "library") await loadFormalDefaultImages({ view: "library" });
    else if (params.get("pick") === "recommended") await loadFormalDefaultImages({ view: "recommended", shotNo: "", matchScope: "", query: "", filters: {}, page: 1 });
  } catch (error) {
    if (state.formalImageEpoch !== epoch || formalRoute().projectId !== projectId || formalSurface() !== "materials") return;
    const prerequisite = ["CURRENT_BRIEF_MISSING", "CREATIVE_STANDARD_MISSING", "CONFIRMED_BRIEF_REQUIRED"]
      .includes(error?.payload?.detail?.code);
    const message = formalError(error);
    elements.formalImageState.textContent = "暂不可用";
    elements.formalImageGateTitle.textContent = prerequisite ? "请先完成并确认需求" : "暂不能读取图片";
    elements.formalImageGateCopy.textContent = prerequisite
      ? "请先在需求页导入并确认资料，再返回处理项目图片。"
      : message;
    elements.formalImageUnderstand.disabled = true;
    elements.formalImageToPlan.setAttribute("aria-disabled", "true");
    elements.formalImageToPlan.tabIndex = -1;
    elements.formalImageGrid.innerHTML = prerequisite
      ? '<div class="ui00-business-empty"><strong>等待需求确认</strong><p>请先在需求页导入并确认资料。</p></div>'
      : `<div class="ui00-business-empty"><strong>图片资料暂不可用</strong><p>${escapeHtml(message)}</p></div>`;
    renderFormalImageWorkspaceError(
      prerequisite ? "等待脚本版本" : "逐镜配图暂不可用",
      prerequisite ? "完成 Brief、图片理解和脚本生成后，可在这里逐镜确认配图。" : message,
    );
  }
}

async function loadFormalShotImages(projectId, { capture = true } = {}) {
  if (capture) captureFormalImageInputs();
  state.formalImageBusy = false;
  state.formalImageAutoBusy = false;
  const epoch = state.formalImageEpoch;
  const active = () => state.formalImageEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "script";
  state.formalImageWorkspace = null;
  state.formalImageProjectId = projectId;
  state.formalImageDirty = false;
  elements.formalShotImageProjectRequired.hidden = Boolean(projectId && state.formalBriefAvailable && state.agentAuth && state.agentWorkspaceId);
  elements.formalShotImageWorkbench.hidden = !elements.formalShotImageProjectRequired.hidden;
  if (!projectId || !state.formalBriefAvailable || !state.agentAuth || !state.agentWorkspaceId) {
    elements.formalShotImageState.textContent = "等待项目";
    return;
  }
  elements.formalShotImageState.textContent = "读取中";
  elements.formalImageVersionForm.hidden = true;
  elements.formalImageVersionState.textContent = "读取中";
  elements.formalImageVersionMessage.textContent = "正在读取当前脚本版本…";
  elements.formalImageShotGrid.replaceChildren();
  elements.formalImageVersionHistory.replaceChildren();
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    if (!active()) return;
    await loadFormalImageWorkspace(projectId, project.project_version, epoch);
    if (!active() || !state.formalImageWorkspace) return;
    elements.formalImageVersionState.textContent = "正在读取图片…";
    await loadFormalShotImagePreviews(projectId, project.project_version);
    if (!active()) return;
    syncCreativeShotVisibility();
    const workspaceState = state.formalImageWorkspace?.state;
    const label = workspaceState === "confirmed" ? "已确认" : state.formalImageWorkspace?.shots?.length ? "待配图确认" : "等待脚本";
    elements.formalImageVersionState.textContent = label;
    elements.formalShotImageState.textContent = label;
  } catch (error) {
    if (!active()) return;
    renderFormalImageWorkspaceError("暂不可用", formalError(error));
    elements.formalShotImageState.textContent = "暂不可用";
  }
}

async function loadFormalShotImagePreviews(projectId, projectVersion) {
  const workspace = state.formalImageWorkspace, epoch = state.formalImageEpoch;
  const active = () => state.formalImageWorkspace === workspace && state.formalImageEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "script";
  const shots = Array.isArray(state.formalImageWorkspace?.shots) ? state.formalImageWorkspace.shots : [];
  await Promise.all(shots.map(async (shot, index) => {
    if (shot.library_candidate?.preview_url) {
      try {
        const url = await requestFormalPreview(formalDefaultPreviewPath(shot.library_candidate, projectVersion), {
          candidate: shot.library_candidate, model: workspace, owner: `formal:${epoch}`, priority: shot.shot_id === creativeState().shotId ? 0 : 1,
        });
        if (!active()) return;
        state.formalImagePreviewUrls.set(`default:${shot.library_candidate_key}`, url);
        updateFormalImageShotPreview(index); renderCreativeCurrentImage();
      } catch (_) { if (active()) shot.preview_error = "暂选图片预览不可用，请重新核对来源。"; }
      return;
    }
    const imageKey = shot.selected_image_key;
    const target = document.querySelector(`#formal-image-shot-preview-${index}`);
    if (!imageKey || !target || !formalImageCandidateHasRealPreview(formalImageSelectedCandidate(shot))) return;
    try {
      const path = `/api/formal/projects/${encodeURIComponent(projectId)}/images/${encodeURIComponent(imageKey)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(projectVersion)}`;
      const url = await requestFormalPreview(path, { candidate: formalImageSelectedCandidate(shot), model: workspace,
        owner: `formal:${epoch}`, priority: shot.shot_id === creativeState().shotId ? 0 : 1 });
      if (!active() || workspace.shots[index]?.selected_image_key !== imageKey) return;
      const currentTarget = document.querySelector(`#formal-image-shot-preview-${index}`);
      if (!currentTarget?.isConnected) return;
      state.formalImagePreviewUrls.set(imageKey, url);
      const display = formalImageDisplay(shot);
      if (display.candidate?.image_key !== imageKey) { renderCreativeCurrentImage(); validateFormalImageSelections(); return; }
      const image = document.createElement("img");
      image.src = url;
      image.alt = `第 ${shot.shot_no} 镜${display.label}`;
      image.width = 1280;
      image.height = 720;
      image.loading = "lazy";
      image.decoding = "async";
      currentTarget.replaceChildren(image);
      renderCreativeCurrentImage();
      if (active()) validateFormalImageSelections();
    } catch (error) {
      if (!active() || workspace.shots[index]?.selected_image_key !== imageKey) return;
      const currentTarget = document.querySelector(`#formal-image-shot-preview-${index}`);
      if (!currentTarget?.isConnected) return;
      if (state.formalImagePreviewUrls.has(imageKey)) updateFormalImageShotPreview(index);
      else currentTarget.innerHTML = `<span>${escapeHtml(formalError(error))}</span>`;
      if (active()) validateFormalImageSelections();
    }
  }));
}

function renderFormalImageWorkspaceError(title, message) {
  state.formalImageWorkspace = null;
  elements.formalImageVersionState.textContent = title;
  elements.formalImageVersionMessage.hidden = false;
  elements.formalImageVersionMessage.textContent = message;
  elements.formalImageVersionForm.hidden = true;
  elements.formalImageShotGrid.replaceChildren();
  elements.formalImageVersionHistory.replaceChildren();
}

function formalImageWorkspacePath(projectId, projectVersion) {
  return `/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(projectVersion)}`;
}

function creativeImageDraftGate() {
  return false;
}

function markCreativeImageChange(shot) {
  if (!shot?.shot_id) return;
  creativeState().imageChanges.add(shot.shot_id);
  shot.explicitly_changed = true;
  state.formalImageDirty = true;
}

function creativeChangedImageShots() {
  return (state.formalImageWorkspace?.shots || []).filter(shot => shot.explicitly_changed);
}

// One save queue per project/version. Receipts cannot overwrite later local input.
const creativeProgressQueues = new Map();
function creativeProgressQueue(workspace = state.formalImageWorkspace, projectId = state.formalImageProjectId) {
  if (!workspace?.base_version_id || !projectId) return null;
  const key = `${state.agentWorkspaceId}:${projectId}:${workspace.base_version_id}`;
  if (!creativeProgressQueues.has(key)) creativeProgressQueues.set(key, {
    projectId, baseVersionId: workspace.base_version_id, model: null, baseline: new Map(),
    desired: new Map(), promise: null, retry: null, error: "", conflict: false, timer: null, scriptRevision: 0, remote: null,
  });
  return creativeProgressQueues.get(key);
}
function creativeProgressSelection(shot) {
  if (!shot.explicitly_changed) return { shot_id: shot.shot_id, selection_active: false, image_key: "", library_candidate_key: "", checked: false, decision_kind: "image_confirmed", temporary_reason: "", reuse_reason: "", query: shot.query_input ?? shot.query ?? "" };
  return { shot_id: shot.shot_id, selection_active: true, image_key: shot.library_candidate_key ? "" : shot.selected_image_key || "",
    library_candidate_key: shot.library_candidate_key || "", checked: Boolean(shot.confirmed),
    decision_kind: shot.decision_kind || "image_confirmed", temporary_reason: shot.temporary_reason || "",
    reuse_reason: shot.reuse_reason || "", query: shot.query || "" };
}
function creativeProgressPending(queue) {
  return queue && [...queue.desired].some(([key, value]) => JSON.stringify(value) !== queue.baseline.get(key));
}
function queueCreativeProgress() {
  const queue = creativeProgressQueue();
  if (!queue?.model) return;
  queue.scriptRevision = Number(state.formalScript?.revision || 0);
  for (const shot of state.formalImageWorkspace.shots || []) {
    if (shot.explicitly_changed || shot.query_input || queue.desired.has(shot.shot_id)) queue.desired.set(shot.shot_id, creativeProgressSelection(shot));
  }
  window.clearTimeout(queue.timer);
  if (creativeProgressPending(queue) && !queue.error && !queue.promise && !queue.confirming) queue.timer = window.setTimeout(() => flushCreativeProgress({ queueOverride: queue }), 700);
  syncCreativeProgressStatus();
}
function creativeImageStatus(shot) {
  if (!shot) return "读取中";
  const status = shot.explicitly_changed
    ? shot.decision_kind === "temporary_text_basis" || !(shot.selected_image_key || shot.library_candidate_key) ? "missing"
      : shot.confirmed && !shot.library_candidate_key && !shot.needs_recheck && !shot.progress_unavailable ? "confirmed" : "selected"
    : shot.progress_status || (shot.confirmed && !shot.image_reconfirmation_required ? "saved" : shot.selected_image_key ? "selected" : "missing");
  return ({missing:"未配图",selected:"暂选待核对",confirmed:"已确认待保存",saved:"已保存"}[status] || "读取中")
    + (shot.progress_unavailable ? " · 图片不可用" : shot.needs_recheck || shot.image_reconfirmation_required ? " · 需重新核对" : "");
}
function syncCreativeProgressStatus() {
  const status = document.getElementById("creative-progress-status");
  const button = document.getElementById("creative-save-progress");
  if (!status || !button) return;
  const queue = creativeProgressQueue();
  const pending = creativeProgressPending(queue) || formalScriptHasPendingWrites();
  status.textContent = queue?.error || (queue?.promise ? "正在保存进度…" : pending ? "有待保存的进度"
    : queue?.model?.saved_at ? `进度已保存 · ${formalScriptDate(queue.model.saved_at)}` : "修改后自动保存进度");
  status.dataset.state = queue?.error ? "error" : pending ? "pending" : "saved";
  button.disabled = Boolean(queue?.promise);
  button.textContent = queue?.error ? "重试保存进度" : "保存进度";
  const shots = state.formalImageWorkspace?.shots || [];
  const selected = shots.filter(shot => shot.confirmed || shot.explicitly_changed && (shot.selected_image_key || shot.library_candidate_key));
  const recheck = selected.filter(shot => !shot.confirmed || shot.library_candidate_key || shot.needs_recheck);
  const summary = document.getElementById("creative-image-summary");
  if (summary) summary.textContent = shots.length ? `已选用 ${selected.length} · 待选 ${shots.length - selected.length} · 待核对 ${recheck.length}` : "";
  elements.creativeShotList?.querySelectorAll("[data-creative-shot]").forEach(button => {
    const shot = shots.find(item => item.shot_id === button.dataset.creativeShot), label = button.querySelector(".creative-shot-progress");
    if (label && shot) label.textContent = creativeImageStatus(shot);
  });
  const stage = !state.formalScript?.shots?.length ? "structure" : state.formalScript?.can_save_version || formalScriptHasPendingWrites() ? "script" : shots.length && shots.every(shot => shot.confirmed && !shot.explicitly_changed) ? "review" : "images";
  elements.creativeWorkspace.querySelectorAll("[data-creative-stage]").forEach(button => { if (button.dataset.creativeStage === stage) button.setAttribute("aria-current", "step"); else button.removeAttribute("aria-current"); });
  const conflict = document.getElementById("creative-progress-conflict");
  if (conflict) conflict.hidden = !queue?.conflict;
}
async function flushCreativeProgress({ manual = false, queueOverride = null } = {}) {
  const queue = queueOverride || creativeProgressQueue();
  if (queue?.confirming) return false;
  if (queue?.promise) return queue.promise;
  if (queue?.conflict) { syncCreativeProgressStatus(); return false; }
  if (queue) window.clearTimeout(queue.timer);
  if (!queue) return flushFormalScriptChanges({ allowRetry: manual });
  if (queue.error && !manual) return false;
  queue.error = "";
  queue.promise = (async () => {
    if (state.formalScriptProjectId === queue.projectId) {
      if (!await flushFormalScriptChanges({ allowRetry: manual })) return false;
      queue.scriptRevision = Number(state.formalScript?.revision || 0);
    }
    if (!queue.model) return true;
    while (queue.retry || creativeProgressPending(queue)) {
      const selections = [...queue.desired].filter(([key, value]) => JSON.stringify(value) !== queue.baseline.get(key)).map(([, value]) => value);
      const body = queue.retry || { base_version_id: queue.baseVersionId,
        expected_project_version: queue.model.project_version, expected_revision: queue.model.revision,
        expected_script_revision: queue.scriptRevision, selections,
        idempotency_key: formalRequestKey("image-progress") };
      queue.retry = body;
      try {
        const model = await api(`/api/formal/projects/${encodeURIComponent(queue.projectId)}/image-workspace/progress?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, { method: "PATCH", body: JSON.stringify(body) });
        queue.retry = null;
        if (Number(model.revision) >= Number(queue.model.revision)) queue.model = model;
        for (const selection of body.selections) queue.baseline.set(selection.shot_id, JSON.stringify(selection));
      } catch (error) {
        const code = error?.payload?.detail?.code;
        queue.conflict = ["IMAGE_PROGRESS_CONFLICT", "PROJECT_VERSION_CONFLICT", "DRAFT_REVISION_CONFLICT"].includes(code);
        queue.error = queue.conflict ? "另一处修改了当前进度。此处输入已保留，请核对后继续。" : `${formalError(error)} 输入已保留，可重试保存。`;
        return false;
      }
    }
    return true;
  })();
  syncCreativeProgressStatus();
  try { const saved = await queue.promise; if (manual && saved) showToast("当前进度已保存"); return saved; }
  finally { queue.promise = null; syncCreativeProgressStatus(); syncFormalScriptSaveControls(state.formalScript); }
}
async function resolveCreativeProgressConflict(action) {
  const queue = creativeProgressQueue();
  if (!queue?.conflict) return;
  const panel = document.getElementById("creative-progress-conflict");
  try {
    if (action === "read") {
      queue.remote = await api(`/api/formal/projects/${encodeURIComponent(queue.projectId)}/image-workspace/progress?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`);
      panel.querySelector("[data-progress-conflict-summary]").textContent = `已保存进度 ${queue.remote.selected_count} 镜；此处保留 ${queue.desired.size} 镜选择。请确定使用哪一份。`;
      panel.querySelector("[data-progress-conflict-local]").hidden = queue.remote.base_version_id !== queue.baseVersionId || queue.remote.script_revision !== state.formalScript?.revision;
      panel.querySelector("[data-progress-conflict-server]").hidden = false;
      return;
    }
    if (!queue.remote) return;
    if (action === "local" && (queue.remote.base_version_id !== queue.baseVersionId || queue.remote.script_revision !== state.formalScript?.revision)) return;
    queue.model = queue.remote; queue.retry = null; queue.error = ""; queue.conflict = false;
    queue.baseline.clear();
    for (const selection of queue.remote.selections || []) queue.baseline.set(selection.shot_id, JSON.stringify({ shot_id: selection.shot_id, selection_active: selection.selection_active !== false,
      image_key: selection.image_key || "", library_candidate_key: selection.library_candidate_key || "", checked: Boolean(selection.checked),
      decision_kind: selection.decision_kind, temporary_reason: selection.temporary_reason || "", reuse_reason: selection.reuse_reason || "", query: selection.query || "" }));
    if (action === "server") {
      queue.desired.clear(); sessionStorage.removeItem(formalImageInputKey(queue.projectId, queue.baseVersionId));
      state.formalImageDirty = false; state.formalImageWorkspace = null;
      await loadFormalShotImages(queue.projectId);
    } else await flushCreativeProgress({ manual: true });
    syncCreativeProgressStatus();
  } catch (error) { panel.querySelector("[data-progress-conflict-summary]").textContent = formalError(error); }
}

async function loadCreativeProgress(value, projectId) {
  const queue = creativeProgressQueue(value, projectId);
  if (!queue || !value.shots?.length) return null;
  const model = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/progress?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`);
  queue.confirming = Boolean(sessionStorage.getItem(creativeImageConfirmationKey(projectId)));
  if (value.base_version_id !== model.base_version_id) throw new Error("版本已变化，请重新读取当前稿");
  const pending = new Map([...queue.desired].filter(([id, selection]) => JSON.stringify(selection) !== queue.baseline.get(id)));
  queue.model = model;
  value.creative_references = model.creative_references || [];
  let transfer = document.querySelector("#creative-reference-transfer");
  if (!transfer) { transfer = document.createElement("div"); transfer.id = "creative-reference-transfer"; elements.formalImageSelectionStatus.insertAdjacentElement("afterend", transfer); }
  const pendingTransfer = model.creative_reference_transfer;
  transfer.innerHTML = pendingTransfer ? `<p>${escapeHtml(pendingTransfer.message)}</p>${pendingTransfer.can_transfer ? '<button type="button" class="secondary-button compact" data-creative-transfer-images>接入初稿参考图</button>' : ""}` : "";
  const transferButton = transfer.querySelector("button");
  if (transferButton) transferButton.onclick = async () => {
    transferButton.disabled = true;
    try {
      await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/creative-references?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, { method:"POST", body: JSON.stringify({version_id:pendingTransfer.version_id}) });
      await loadFormalImageWorkspace(projectId, value.project_version);
    } catch (error) { showToast(formalError(error)); transferButton.disabled = false; }
  };
  queue.scriptRevision = model.script_revision || 0;
  for (const selection of model.selections || []) {
    const shot = value.shots.find(item => item.shot_id === selection.shot_id);
    if (!shot) continue;
    applyCreativeProgressSelection(shot, selection);
    const input = creativeProgressSelection(shot);
    queue.baseline.set(shot.shot_id, JSON.stringify(input));
    if (!pending.has(shot.shot_id)) queue.desired.set(shot.shot_id, input);
  }
  for (const [id, selection] of pending) {
    const shot = value.shots.find(item => item.shot_id === id);
    if (shot) applyCreativeProgressSelection(shot, selection);
  }
  return model;
}
function applyCreativeProgressSelection(shot, selection) {
  if (selection.selection_active === false) { shot.query_input = selection.query || ""; return; }
  if (selection.candidate && selection.image_key && !shot.candidates?.some(item => item.image_key === selection.image_key)) {
    shot.candidates = [...(shot.candidates || []), selection.candidate];
  }
  shot.selected_image_key = selection.image_key || "";
  shot.library_candidate_key = selection.library_candidate_key || "";
  shot.library_candidate = selection.library_candidate_key ? selection.candidate : null;
  shot.confirmed = Boolean(selection.checked && !selection.needs_recheck && !selection.unavailable);
  shot.progress_unavailable = Boolean(selection.unavailable);
  shot.needs_recheck = Boolean(selection.needs_recheck);
  for (const field of ["decision_kind", "temporary_reason", "reuse_reason", "query"]) shot[field] = selection[field] || (field === "decision_kind" ? "image_confirmed" : "");
  shot.query_input = shot.query;
  shot.explicitly_changed = true;
  state.formalImageDirty = true;
}

function formalImageInputKey(projectId, baseVersionId) {
  return `mplus.image-inputs.${state.agentWorkspaceId}.${projectId}.${baseVersionId}`;
}

function rememberFormalDefaultAdoptions(projectId, response = {}) {
  if (!response.base_version_id || !response.adopted_selections?.length) return;
  try {
    const key = formalImageInputKey(projectId, response.base_version_id);
    const saved = JSON.parse(sessionStorage.getItem(key) || "null") || { project_version: response.project_version, shots: [] };
    for (const selection of response.adopted_selections) {
      if (!selection.shot_no || !selection.image_key || !selection.content_sha256) continue;
      const index = saved.shots.findIndex(shot => String(shot.shot_no) === String(selection.shot_no));
      const mappedShot = (state.formalImageWorkspace?.shots || state.formalScript?.shots || []).find(shot => String(shot.shot_no) === String(selection.shot_no));
      if (!selection.shot_id && !mappedShot?.shot_id) continue;
      const input = { shot_no: String(selection.shot_no), shot_id: selection.shot_id || mappedShot.shot_id, explicitly_changed: true, selected_image_key: selection.image_key,
        adopted_content_sha256: selection.content_sha256, decision_kind: "image_confirmed",
        confirmed: false, temporary_reason: "", reuse_reason: "", query: "", query_input: "" };
      if (index < 0) saved.shots.push(input);
      else saved.shots[index] = input;
    }
    sessionStorage.setItem(key, JSON.stringify(saved));
  } catch (_error) { showToast("图片已采用，请在逐镜页重新选择。 "); }
}

function invalidateFormalImageVersionProposal({ announce = true } = {}) {
  state.formalImageInputRevision += 1;
  if (state.formalScriptRevisionProposal?.response?.type === "action_proposal" && !state.formalScriptRevisionProposal.response.draft_only) {
    invalidateFormalScriptProposal();
  }
  const hadProposal = Boolean(state.formalImageVersionProposal);
  state.formalImageVersionProposal = null;
  elements.formalImageVersionProposal.hidden = true;
  elements.formalImageVersionProposal.replaceChildren();
  if (hadProposal && announce) showToast("配图已变化，请重新预览版本影响。");
}

function captureFormalImageInputs() {
  const workspace = state.formalImageWorkspace;
  if (!workspace?.base_version_id || !state.formalImageProjectId) return;
  let changed = false;
  for (const [index, shot] of (workspace.shots || []).entries()) {
    for (const [name, key] of [["reason", "temporary_reason"], ["reuse-reason", "reuse_reason"], ["confirm", "confirmed"], ["query", "query_input"]]) {
      const field = elements.formalImageShotGrid.querySelector(`[data-image-shot-${name}="${index}"]`);
      if (!field) continue;
      const value = key === "confirmed" ? Boolean(field.checked) : field.value;
      const previous = key === "confirmed" ? Boolean(shot[key]) : String(shot[key] || "");
      if (value !== previous) { changed = true; if (key !== "query_input") markCreativeImageChange(shot); }
      shot[key] = value;
    }
  }
  if (changed) {
    state.formalImageDirty = true;
    invalidateFormalImageVersionProposal();
  }
  queueCreativeProgress();
  if (!state.formalImageDirty) return;
  try {
    sessionStorage.setItem(formalImageInputKey(state.formalImageProjectId, workspace.base_version_id), JSON.stringify({
      project_version: workspace.project_version, progress_revision: creativeProgressQueue()?.model?.revision || 0,
      shots: workspace.shots.map((shot) => ({
        shot_no: String(shot.shot_no), shot_id: shot.shot_id, explicitly_changed: Boolean(shot.explicitly_changed),
        selected_image_key: shot.selected_image_key, decision_kind: shot.decision_kind,
        temporary_reason: shot.temporary_reason, reuse_reason: shot.reuse_reason,
        confirmed: shot.confirmed, query: shot.query, query_input: shot.query_input,
        candidate_snapshot_digest: shot.candidate_snapshot_digest,
      })),
    }));
  } catch (_error) { /* The live form remains usable if browser storage is full. */ }
}

function restoreFormalImageInputs(workspace, projectId) {
  state.formalImageDirty = false;
  const pendingQueries = [];
  if (!workspace?.base_version_id) return pendingQueries;
  try {
    const saved = JSON.parse(sessionStorage.getItem(formalImageInputKey(projectId, workspace.base_version_id)) || "null");
    if (!saved) return pendingQueries;
    const serverRevision = creativeProgressQueue(workspace, projectId)?.model?.revision || 0;
    if (serverRevision > 0) return pendingQueries;
    const sameVersion = saved.project_version === workspace.project_version;
    for (const shot of workspace.shots || []) {
      const prior = (saved.shots || []).find((item) => shot.shot_id && item.shot_id && shot.shot_id === item.shot_id);
      if (!prior) continue;
      if (!prior.explicitly_changed && prior.decision_kind === shot.decision_kind && prior.selected_image_key === shot.selected_image_key && String(prior.query || "") === String(shot.query || "")) {
        shot.query_input = prior.query_input ?? prior.query ?? "";
        if (shot.query_input !== String(shot.query || "")) state.formalImageDirty = true;
        continue;
      }
      if (prior.explicitly_changed) markCreativeImageChange(shot);
      if (prior.adopted_content_sha256 && !(shot.candidates || []).some(candidate => candidate.image_key === prior.selected_image_key && candidate.content_sha256 === prior.adopted_content_sha256)) {
        shot.selected_image_key = "";
        shot.confirmed = false;
        state.formalImageDirty = true;
        continue;
      }
      // Cached inputs never replace the current server candidate list or its fingerprint.
      shot.temporary_reason = prior.temporary_reason || "";
      shot.reuse_reason = prior.reuse_reason || "";
      shot.query_input = prior.query_input ?? prior.query ?? "";
      if (["temporary_text_basis", "image_confirmed"].includes(prior.decision_kind)) shot.decision_kind = prior.decision_kind;
      const selected = (shot.candidates || []).some((candidate) => candidate.image_key === prior.selected_image_key);
      shot.selected_image_key = selected ? prior.selected_image_key : "";
      const sameQuery = String(prior.query || "") === String(shot.query || "");
      const sameDigest = Boolean(prior.candidate_snapshot_digest) && prior.candidate_snapshot_digest === shot.candidate_snapshot_digest;
      shot.confirmed = Boolean(prior.confirmed && sameVersion && sameQuery && sameDigest && (selected || shot.decision_kind === "temporary_text_basis"));
      if (!sameQuery || (prior.selected_image_key && !selected && shot.decision_kind !== "temporary_text_basis")) pendingQueries.push({ shot, prior, sameVersion });
      state.formalImageDirty = true;
    }
    if (state.formalImageDirty) showToast("已保留配图输入；更新的候选需重新确认。");
  } catch (_error) { /* Ignore an incomplete local draft, keeping server data. */ }
  return pendingQueries;
}

function focusFormalImageShot(index, { select = true } = {}) {
  const shot = state.formalImageWorkspace?.shots?.[index];
  if (select && shot?.shot_id) selectCreativeShot(shot.shot_id);
  const card = elements.formalImageShotGrid.querySelector(`[data-image-shot="${index}"]`);
  const jump = document.querySelector("#formal-image-jump");
  if (card && jump) jump.value = String(index);
  card?.focus({ preventScroll: true });
  card?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function formalImageDecisionIssue(shot, { requirePreview = true } = {}) {
  const temporary = shot.decision_kind === "temporary_text_basis";
  if (temporary) return "等待补图，仅保存进度";
  if (shot.library_candidate_key) return "创意库图片待正式采用";
  if (shot.progress_unavailable) return "所选图片已不可用，请重新选择";
  if (!temporary && !shot.selected_image_key) return "选择图片";
  if (requirePreview && !shot.confirmed && !temporary && !state.formalImagePreviewUrls.has(shot.selected_image_key)) return "等待图片预览";
  if (!temporary && shot.reuse_required && !String(shot.reuse_reason || "").trim()) return "填写复用理由";
  if (!shot.confirmed) return "确认本镜";
  return "";
}

function useFormalImageShot(index) {
  const shot = state.formalImageWorkspace?.shots?.[index];
  if (!shot || state.formalImageVersionBusy) return;
  if (shot.library_candidate_key) { void openCreativeImagePicker("library", shot.shot_id); return; }
  markCreativeImageChange(shot);
  captureFormalImageInputs();
  refreshFormalImageReuseState();
  const issue = formalImageDecisionIssue(shot);
  if (issue && issue !== "确认本镜") { showToast(`第 ${shot.shot_no} 镜：${issue}`); return; }
  const confirmation = elements.formalImageShotGrid.querySelector(`[data-image-shot-confirm="${index}"]`);
  if (confirmation) confirmation.checked = true;
  validateFormalImageSelections();
  const shots = state.formalImageWorkspace.shots;
  const allowed = new Set(creativeAllowedShotIds());
  const next = shots.findIndex((item, position) => position > index && allowed.has(item.shot_id) && formalImageDecisionIssue(item));
  const remaining = next >= 0 ? next : shots.findIndex((item) => allowed.has(item.shot_id) && formalImageDecisionIssue(item));
  if (remaining >= 0) focusFormalImageShot(remaining);
  else {
    showToast(`本次范围 ${allowed.size} 镜已处理，进度将自动保存。`);
    elements.formalImageVersionSave.focus({ preventScroll: true });
    elements.formalImageVersionSave.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function formalImageReuseTargets(shot) {
  if (!shot?.selected_image_key || shot.library_candidate_key || shot.progress_unavailable) return [];
  const identity = item => (item.candidates || []).find(candidate => candidate.image_key === item.selected_image_key)
    || (item.current_image?.image_key === item.selected_image_key ? item.current_image : null);
  const source = identity(shot);
  return (state.formalImageWorkspace?.shots || []).filter(item => {
    if (item.shot_id === shot.shot_id || !item.selected_image_key || item.library_candidate_key
      || item.progress_unavailable || item.decision_kind === "temporary_text_basis") return false;
    const candidate = identity(item);
    return item.selected_image_key === shot.selected_image_key
      || Boolean(source?.content_sha256 && candidate?.content_sha256 === source.content_sha256);
  });
}

function applyFormalImageReuseReason(index) {
  if (state.formalImageVersionBusy || state.formalScriptBusy) return;
  captureFormalImageInputs();
  const shot = state.formalImageWorkspace?.shots?.[index];
  const reason = String(shot?.reuse_reason || "").trim();
  if (!reason) return;
  const targets = formalImageReuseTargets(shot).filter(item => String(item.reuse_reason || "").trim() !== reason);
  if (!targets.length) { showToast("其他同图镜头已使用相同理由，无需重复修改。"); return; }
  const conflicts = targets.filter(item => String(item.reuse_reason || "").trim());
  const preview = conflicts.map(item => `第 ${item.shot_no} 镜：${item.reuse_reason}`).join("\n");
  if (!window.confirm(`将理由应用到另外 ${targets.length} 镜（${targets.map(item => item.shot_no).join("、")}）？\n${reason}${conflicts.length ? `\n以下已有理由将替换：\n${preview}` : ""}`)) return;
  for (const item of targets) {
    item.reuse_reason = reason;
    markCreativeImageChange(item);
    const position = state.formalImageWorkspace.shots.indexOf(item);
    const input = elements.formalImageShotGrid.querySelector(`[data-image-shot-reuse-reason="${position}"]`);
    if (input) input.value = reason;
  }
  invalidateFormalImageVersionProposal();
  validateFormalImageSelections();
  showToast(`理由已应用到另外 ${targets.length} 镜，图片核对状态保持不变。`);
}

function formalImageCandidateLabel(candidate, index) {
  if (!candidate) return `项目图片 ${index + 1}`;
  const score = candidate.score ?? candidate.match_explanation?.total_score;
  return `${candidate.caption || `项目图片 ${index + 1}`}${score === undefined ? "" : ` · ${score} 分`}`;
}

function formalVisibleImageCandidates(candidates, keyField, selectedKeys, expanded = false) {
  return candidates.filter((candidate) => expanded || !candidate.duplicate_of || selectedKeys.includes(candidate[keyField]));
}

function formalImageShotOptions(shot) {
  const candidates = formalVisibleImageCandidates(shot.candidates || [], "image_key", [shot.selected_image_key], Boolean(shot.show_duplicate_sources));
  const options = candidates.map((candidate, index) => `<option value="${escapeHtml(candidate.image_key)}"${candidate.image_key === shot.selected_image_key ? " selected" : ""}${formalImageCandidateHasRealPreview(candidate) ? "" : " disabled"}>${escapeHtml(formalImageCandidateLabel(candidate, index))}${candidate.duplicate_count ? ` · 同图 ${Number(candidate.duplicate_count) + 1} 个来源` : ""}${formalImageCandidateHasRealPreview(candidate) ? "" : " · 无可用预览"}</option>`).join("");
  return options ? `${shot.selected_image_key ? "" : '<option value="" selected>请选择图片</option>'}${options}` : '<option value="">先选创意库图片或上传图片</option>';
}

const creativeLibraryFacetNames = [
  ["source", "来源"], ["scene", "场景"], ["subject", "主体"],
  ["brand", "品牌"], ["framing", "景别"], ["analysis_status", "分析状态"],
];

function creativePickerCandidatesPath(picker) {
  const params = new URLSearchParams({ workspace_id: state.agentWorkspaceId,
    expected_project_version: String(picker.projectVersion), view: picker.source,
    match_scope: picker.source === "recommended" ? (picker.shotNo || picker.blockId ? "current_shot" : "") : picker.matchScope || "all",
    query: picker.query || "", page: String(picker.page || 1), page_size: "12", shot_no: String(picker.shotNo || ""), ...(picker.blockId ? {block_id: picker.blockId} : {}) });
  for (const [name] of creativeLibraryFacetNames) if (picker.filters?.[name]) params.set(name, picker.filters[name]);
  return `/api/formal/projects/${encodeURIComponent(picker.projectId)}/images/default-candidates?${params}`;
}

function resetCreativePickerFilters(picker) {
  picker.query = ""; picker.page = 1; picker.matchScope = "all"; picker.filters = {};
}

function creativePickerCandidateGroups(candidates) {
  const groups = new Map();
  for (const candidate of candidates) {
    const key = candidate.duplicate_group_id || candidate.duplicate_of || candidate.image_key || candidate.candidate_key;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(candidate);
  }
  return [...groups.values()].map(values => [...values.filter(item => !item.duplicate_of), ...values.filter(item => item.duplicate_of)]);
}

function creativeLibraryCountText(model) {
  return `库存 ${Number(model.inventory_records ?? model.candidate_count ?? 0)} 条 · ${Number(model.inventory_groups ?? model.total_groups ?? 0)} 组图片 · 筛选后 ${Number(model.filtered_groups ?? model.total_groups ?? 0)} 组 · 本页 ${Number(model.page_groups ?? model.displayed_groups ?? 0)} 组 / ${Number(model.page_records ?? model.displayed_images ?? 0)} 条 · 第 ${Number(model.page || 1)} 页`;
}

function creativeLibraryFiltersMarkup(model, filters, { prefix = "picker", matchScope = "all", hasShot = true, recommended = false, expanded = false } = {}) {
  const selectedCount = creativeLibraryFacetNames.filter(([name]) => filters?.[name]).length;
  const scope = recommended ? "" : `<label class="creative-picker-match-scope"><span>浏览范围</span><select data-${prefix}-match-scope><option value="all"${matchScope === "all" ? " selected" : ""}>全部图片</option><option value="current_shot"${matchScope === "current_shot" ? " selected" : ""}${hasShot ? "" : " disabled"}>适合本镜</option></select></label>`;
  const facets = creativeLibraryFacetNames.map(([name, label]) => {
    const choices = model.facets?.[name] || [];
    return `<label><span>${label}</span><select data-${prefix}-filter="${name}" aria-label="按${label}筛选"><option value="">全部${label}</option>${choices.map(option => `<option value="${escapeHtml(option.value)}"${filters?.[name] === option.value ? " selected" : ""}>${escapeHtml(option.label)}${Number.isFinite(Number(option.count)) ? `（${Number(option.count)}）` : ""}</option>`).join("")}</select></label>`;
  }).join("");
  return `${scope}<details class="creative-picker-filter-panel"${expanded || selectedCount ? " open" : ""}><summary>筛选条件${selectedCount ? `（已选 ${selectedCount} 项）` : ""}</summary><div class="creative-picker-filters">${facets}</div></details>`;
}

function creativePickerImageKey(candidate) {
  return candidate?.image_key || (candidate?.candidate_key ? `default:${candidate.candidate_key}` : "");
}

function creativePickerPreviewPath(picker, candidate) {
  return candidate.image_key
    ? `/api/formal/projects/${encodeURIComponent(picker.projectId)}/images/${encodeURIComponent(candidate.image_key)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${picker.projectVersion}`
    : formalDefaultPreviewPath(candidate, picker.projectVersion);
}

function renderCreativePickerResults(picker, model) {
  const groups = creativePickerCandidateGroups(picker.candidates);
  const renderCard = item => {
    const index = picker.candidates.indexOf(item), selected = creativePickerImageKey(picker.selected) === creativePickerImageKey(item);
    const caption = item.actual_caption || item.caption || "图片内容待识别";
    const url = formalPreviewDisplayUrl(creativePickerImageKey(item));
    const sourceLabel = item.source ? planSourceLabel(item.source) : item.sources?.length ? formalImageSourceLabel(item.sources[0]) : item.source_status_label || "来源待核对";
    return `<div class="creative-picker-card-wrap"><button type="button" class="creative-picker-card${selected ? " is-selected" : ""}" data-picker-candidate="${index}" aria-pressed="${selected}" aria-label="查看图片：${escapeHtml(caption)}"><span class="creative-picker-pixel" data-picker-pixel="${index}">${url ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(caption)}" decoding="async">` : "读取缩略图中"}${item.is_animated ? '<span class="creative-picker-animation">动图</span>' : ""}</span><strong title="${escapeHtml(caption)}">${escapeHtml(caption)}</strong><small class="creative-picker-source" title="${escapeHtml(sourceLabel)}">${escapeHtml(sourceLabel)}</small></button><div data-picker-card-status="${index}"></div></div>`;
  };
  const cards = groups.map((values, groupIndex) => `<article class="creative-picker-group">${renderCard(values[0])}${values.length > 1 ? `<details class="creative-picker-duplicates" data-picker-duplicates="${groupIndex}"${values.slice(1).some(item => creativePickerImageKey(item) === creativePickerImageKey(picker.selected)) ? " open" : ""}><summary>同图 ${values.length} 个来源</summary>${values.slice(1).map(renderCard).join("")}</details>` : ""}</article>`).join("");
  const count = picker.source === "project"
    ? `本镜项目候选 ${groups.length} 组 / ${picker.candidates.length} 条`
    : picker.source === "recommended"
      ? `${model.filtered_groups == null ? "" : `筛选后 ${Number(model.filtered_groups)} 组 · `}本次推荐 ${groups.length} 组图片 / ${picker.candidates.length} 条候选记录`
      : creativeLibraryCountText(model);
  return `<p class="creative-picker-counts" role="status">${escapeHtml(count)}</p><p class="creative-preview-status" data-picker-preview-progress role="status">缩略图：已加载 0 · 加载中 0 · 失败 0</p>${picker.source === "recommended" ? '<p class="creative-preview-status">智能推荐按本镜排序；完整库存可在创意库浏览。</p>' : ""}<div class="creative-picker-grid">${cards || '<p>没有匹配图片，清除筛选、换个关键词或上传图片。</p>'}</div>${picker.source === "library" ? `<nav class="creative-picker-pages" aria-label="图库翻页"><button type="button" class="secondary-button compact" data-picker-page="${picker.page - 1}"${picker.page <= 1 ? " disabled" : ""}>上一页</button><span>第 ${picker.page} 页${model.total_groups ? ` / ${Math.ceil(model.total_groups / 12)} 页` : ""}</span><button type="button" class="secondary-button compact" data-picker-page="${picker.page + 1}"${model.has_more ? "" : " disabled"}>下一页</button></nav>` : ""}`;
}

let firstCreativePicker = null;
function firstPickerStorageKey(view) { return `mplus.first-images.${view.workspaceId}.${view.projectId}.${view.data?.draft_id || 'draft'}`; }
function firstPickerIdentity(item) { return item.library_candidate_key || item.candidate_key || item.image_key || item.task_id || item.reference_id; }
function firstPickerKeep(picker) {
  const unassigned = picker.selections.some(item => !picker.view.data.blocks.some(block => block.block_id === item.block_id));
  if (!picker.touched.size && !picker.pending && !unassigned) {
    try { sessionStorage.removeItem(firstPickerStorageKey(picker.view)); } catch (_) {}
    return;
  }
  try { sessionStorage.setItem(firstPickerStorageKey(picker.view), JSON.stringify({revision: picker.revision, projectVersion: picker.projectVersion, selections: picker.selections, touched: [...picker.touched], pending: picker.pending, needsRebase: Boolean(picker.needsRebase), unassigned})); } catch (_) {}
}
function firstPickerClose(picker = firstCreativePicker) {
  if (!picker || firstCreativePicker !== picker) return;
  firstPickerKeep(picker); picker.controller?.abort();
  formalPreviewQueue?.release(picker.owner);
  picker.panel.remove(); firstCreativePicker = null;
  document.body.classList.remove('creative-picker-open');
  if (picker.opener?.isConnected) picker.opener.focus({preventScroll: true});
}
function firstPickerActive(picker) { return firstCreativePicker === picker && formalRoute().page === 'script'
  && formalRoute().projectId === picker.projectId && picker.view.auth === state.agentAuth && picker.view.workspaceId === state.agentWorkspaceId
  && picker.location === `${window.location.pathname}${window.location.search}` && picker.panel.isConnected; }
function firstPickerNormalizePrimary(picker, blockId) {
  const assigned = picker.selections.filter(item => item.block_id === blockId);
  if (!assigned.some(item => item.primary) && assigned[0]) assigned[0].primary = true;
}
function firstPickerPreview(picker, item, original = false) {
  if (!item.preview_url && !item.image_key) return Promise.reject(new Error('此图片暂未提供预览'));
  const path = item.image_key ? `/api/formal/projects/${encodeURIComponent(picker.projectId)}/images/${encodeURIComponent(item.image_key)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${picker.projectVersion}` : item.preview_url;
  return requestFormalPreview(path, {candidate: item, model: {project_version: picker.projectVersion}, owner: picker.owner, variant: original ? 'original' : 'thumbnail', priority: original ? 0 : 1});
}
function renderFirstPickerAssignments(picker) {
  if (!firstPickerActive(picker)) return;
  const blocks = picker.view.data.blocks, panel = picker.panel;
  if (!blocks.some(block => block.block_id === picker.blockId)) picker.blockId = blocks[0]?.block_id || '';
  panel.querySelector('[data-first-picker-target]').innerHTML = blocks.map(block => `<option value="${escapeHtml(block.block_id)}"${block.block_id === picker.blockId ? ' selected' : ''}>第 ${escapeHtml(block.shot_no)} 段 · ${escapeHtml(block.fields['段落'] || '')}</option>`).join('');
  const target = blocks.find(block => block.block_id === picker.blockId);
  panel.querySelector('[data-first-picker-intent]').textContent = target?.fields['画面描述'] || '';
  const totalTargets = new Set(picker.selections.map(item => item.block_id)).size;
  panel.querySelector('[data-first-picker-selection-count]').textContent = `已选 ${picker.selections.length} 张 · 分配到 ${totalTargets} 段；下方逐张确认目标段落和主图。`;
  panel.querySelector('.first-picker-assignments').innerHTML = picker.selections.map((item, index) => {
    const block = blocks.find(entry => entry.block_id === item.block_id);
    return `<div class="first-picker-assignment"><span data-first-picked-preview="${index}"></span><div><strong>${escapeHtml(item.caption || item.origin_label || '已选图片')}</strong><small>${escapeHtml(block?.fields['画面描述'] || '原段落已不可用，请重新分配')}</small></div><label><span class="sr-only">第 ${index + 1} 张图片用于段落</span><select data-first-picker-assign="${index}">${!block ? '<option value="">请选择目标段落</option>' : ''}${blocks.map(row => `<option value="${escapeHtml(row.block_id)}"${row.block_id === item.block_id ? ' selected' : ''}>第 ${escapeHtml(row.shot_no)} 段 · ${escapeHtml(row.fields['段落'] || row.fields['画面描述'])}</option>`).join('')}</select></label><div><label><input type="radio" name="primary-${escapeHtml(item.block_id)}" data-first-picker-primary="${index}"${item.primary ? ' checked' : ''}>主图</label><button type="button" class="text-button" data-first-picker-remove="${index}">移除</button></div></div>`;
  }).join('') || '<p>选择图片后，在这里分配到具体脚本文本。同一段可保留多张参考图。</p>';
  picker.selections.forEach((item, index) => {
    const host = panel.querySelector(`[data-first-picked-preview="${index}"]`);
    void firstPickerPreview(picker, item).then(url => { if (host?.isConnected) host.innerHTML = `<img src="${escapeHtml(url)}" alt="${escapeHtml(item.caption || item.origin_label || '已选图片')}">`; }).catch(() => { if (host?.isConnected) host.textContent = '预览待重试'; });
  });
  panel.querySelector('[data-first-picker-rebase]').hidden = Boolean(picker.pending) || !picker.needsRebase && picker.revision === picker.view.data.revision && picker.projectVersion === picker.view.data.project_version;
  panel.querySelector('[data-first-picker-save]').disabled = picker.busy || !picker.touched.size;
  panel.querySelector('[data-first-picker-save]').textContent = picker.pending ? '读取原保存结果 / 重试' : `保存 ${picker.touched.size} 段配图`;
  panel.querySelectorAll('[data-first-picker-candidate]').forEach(button => {
    const candidate = picker.candidates[Number(button.dataset.firstPickerCandidate)];
    const selected = picker.selections.some(item => item.block_id === picker.blockId && firstPickerIdentity(item) === firstPickerIdentity(candidate));
    button.setAttribute('aria-pressed', String(selected)); button.classList.toggle('is-selected', selected);
  });
  firstPickerKeep(picker);
}
async function loadFirstPicker(picker) {
  if (!firstPickerActive(picker)) return;
  picker.controller?.abort(); picker.controller = new AbortController();
  const request = ++picker.request, panel = picker.panel, results = panel.querySelector('.creative-picker-results');
  results.innerHTML = '<p role="status">正在读取图片… 你的已选图片会保留。</p><button type="button" class="text-button" data-first-picker-reload>重新读取</button>';
  panel.querySelectorAll('[data-first-picker-source]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.firstPickerSource === picker.source)));
  try {
    let model, candidates;
    if (picker.source === 'project') {
      model = await api(formalImageCollectionPath(picker.projectId, picker.projectVersion), {signal: picker.controller.signal});
      candidates = (model.images || []).map(item => ({...item, content_sha256: item.content_sha256 || item.image_sha256,
        caption: item.summary?.caption || item.source_name || '项目图片', preview_url: `/api/formal/projects/${encodeURIComponent(picker.projectId)}/images/${encodeURIComponent(item.image_key)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${picker.projectVersion}`}));
    } else {
      model = await api(creativePickerCandidatesPath(picker), {signal: picker.controller.signal});
      candidates = picker.source === 'library' ? model.library_candidates || [] : model.candidates || model.shots?.find(item => item.block_id === picker.blockId)?.candidates || [];
    }
    if (!firstPickerActive(picker) || request !== picker.request) return;
    picker.model = model; picker.candidates = candidates;
    panel.querySelector('.creative-picker-filter-host').innerHTML = picker.source === 'project' ? '' : creativeLibraryFiltersMarkup(model, picker.filters, {prefix: 'first-picker', matchScope: picker.matchScope, hasShot: true, recommended: picker.source === 'recommended'});
    const groups = creativePickerCandidateGroups(picker.candidates);
    const card = item => {
      const index = picker.candidates.indexOf(item);
      const selected = picker.selections.some(entry => entry.block_id === picker.blockId && firstPickerIdentity(entry) === firstPickerIdentity(item));
      return `<article class="creative-picker-group"><button type="button" class="creative-picker-card${selected ? ' is-selected' : ''}" data-first-picker-candidate="${index}" aria-pressed="${selected}" disabled><span class="creative-picker-pixel" data-first-picker-preview="${index}">读取缩略图中</span><strong>${escapeHtml(item.caption || item.actual_caption || '图片内容待核对')}</strong><small>选择到当前段落</small></button><div data-first-picker-preview-status="${index}"></div>${item.source || item.sources ? formalDefaultCandidateDetails(item) : ''}${item.brand_warning ? `<p>${escapeHtml(item.brand_warning)}</p>` : ''}</article>`;
    };
    results.innerHTML = `<p role="status">${picker.source === 'library' ? escapeHtml(creativeLibraryCountText(model)) : `找到 ${groups.length} 组图片`}</p><div class="creative-picker-grid">${groups.map(items => `${card(items[0])}${items.length > 1 ? `<details><summary>同图 ${items.length} 个来源</summary>${items.slice(1).map(card).join('')}</details>` : ''}`).join('') || '<p>没有匹配图片。可浏览完整创意库、修改筛选，或上传项目图片。</p>'}</div>${picker.source === 'library' ? `<nav class="creative-picker-pages" aria-label="初稿图库翻页"><button type="button" class="secondary-button compact" data-first-picker-page="${picker.page - 1}"${picker.page <= 1 ? ' disabled' : ''}>上一页</button><span>第 ${picker.page} 页</span><button type="button" class="secondary-button compact" data-first-picker-page="${picker.page + 1}"${model.has_more ? '' : ' disabled'}>下一页</button></nav>` : ''}`;
    picker.candidates.forEach((item, index) => {
      const host = panel.querySelector(`[data-first-picker-preview="${index}"]`);
      void firstPickerPreview(picker, item).then(url => { if (host?.isConnected) { host.innerHTML = `<img src="${escapeHtml(url)}" alt="${escapeHtml(item.caption || '图片预览')}">`; panel.querySelector(`[data-first-picker-candidate="${index}"]`).disabled = false; } }).catch(error => { if (host?.isConnected) { host.textContent = '图片暂未加载'; panel.querySelector(`[data-first-picker-preview-status="${index}"]`).innerHTML = formalPreviewFailureMarkup(error, `data-first-picker-preview-retry="${index}"`); } });
    });
    renderFirstPickerAssignments(picker);
  } catch (error) {
    if (firstPickerActive(picker) && request === picker.request && error.name !== 'AbortError') results.innerHTML = `<p role="alert">图库读取未完成：${escapeHtml(formalError(error))}。已选图片和分配保留。</p><button type="button" class="secondary-button compact" data-first-picker-reload>重试读取</button>`;
  }
}
async function saveFirstPicker(picker) {
  if (!firstPickerActive(picker) || picker.busy || !picker.touched.size) return;
  picker.busy = true; renderFirstPickerAssignments(picker);
  const status = picker.panel.querySelector('[data-first-picker-status]');
  status.textContent = '正在保存图片与段落的对应关系…';
  try {
    if (!picker.pending) {
      const targets = [...picker.touched];
      const selections = picker.selections.filter(item => picker.touched.has(item.block_id));
      await Promise.all(selections.map(item => firstPickerPreview(picker, item, true)));
      if (!firstPickerActive(picker)) return;
      picker.pending = {expected_project_version: picker.projectVersion, expected_revision: picker.revision, operation_key: crypto.randomUUID(), replace_block_ids: targets,
        selections: selections.map(item => ({block_id: item.block_id, ...(item.library_candidate_key || item.candidate_key ? {library_candidate_key: item.library_candidate_key || item.candidate_key} : item.image_key ? {image_key: item.image_key} : {task_id: item.task_id}), content_sha256: item.content_sha256 || item.image_sha256, primary: Boolean(item.primary)}))};
      firstPickerKeep(picker);
    }
    const result = await api(firstCreativePath(picker.view, 'image-references'), {method: 'POST', body: JSON.stringify(picker.pending)});
    acceptFirstCreative(picker.view, result);
    picker.touched.clear(); picker.pending = null;
    try { sessionStorage.removeItem(firstPickerStorageKey(picker.view)); } catch (_) {}
    picker.selections = result.image_references || [];
    const active = firstPickerActive(picker);
    firstPickerClose(picker); picker.view.renderKey = ''; if (firstCreativeVisible(picker.view)) renderFirstCreative(picker.view);
    if (active) showToast('配图已保存到对应段落，可继续逐段核对。');
  } catch (error) {
    if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) picker.pending = null;
    if (error.status === 409) picker.needsRebase = true;
    if (status.isConnected) status.textContent = `${formalError(error)}。已选图片和分配仍保留；保存前会核对当前稿。`;
    firstPickerKeep(picker);
  } finally { picker.busy = false; if (firstPickerActive(picker)) renderFirstPickerAssignments(picker); }
}
async function openFirstCreativeImagePicker(blockId) {
  const view = firstCreativeView();
  if (!await saveFirstCreative(view)) return;
  const block = view.data?.blocks?.find(item => item.block_id === blockId); if (!block) return;
  firstPickerClose();
  let retained = {}; try { retained = JSON.parse(sessionStorage.getItem(firstPickerStorageKey(view)) || '{}'); } catch (_) {}
  if (!retained.pending && !retained.touched?.length && !retained.unassigned) retained = {};
  const picker = {view, projectId: view.projectId, projectVersion: view.data.project_version, revision: view.data.revision,
    location: `${window.location.pathname}${window.location.search}`,
    blockId, source: 'recommended', query: '', page: 1, matchScope: 'current_shot', filters: {}, request: 0, candidates: [],
    selections: retained.selections || [...view.data.image_references || []],
    touched: new Set(retained.touched || []), pending: retained.pending || null, needsRebase: Boolean(retained.needsRebase),
    opener: document.activeElement, owner: `first-picker:${view.projectId}:${view.data.revision}`, busy: false};
  if ((picker.touched.size || picker.pending || retained.unassigned) && Number.isInteger(retained.revision)) { picker.revision = retained.revision; picker.projectVersion = retained.projectVersion ?? picker.projectVersion; }
  const panel = document.createElement('div'); picker.panel = panel; panel.id = 'first-creative-image-picker'; panel.className = 'creative-picker-backdrop';
  panel.innerHTML = `<section class="creative-picker" role="dialog" aria-modal="true" aria-labelledby="first-picker-title"><header><div><span>为脚本文本选图</span><h2 id="first-picker-title">选择图片与目标段落</h2></div><button type="button" class="secondary-button compact" data-first-picker-close>关闭</button></header><label class="ui00-library-target"><span>当前目标段落</span><select data-first-picker-target>${view.data.blocks.map(item => `<option value="${escapeHtml(item.block_id)}"${item.block_id === blockId ? ' selected' : ''}>第 ${escapeHtml(item.shot_no)} 段 · ${escapeHtml(item.fields['段落'] || '')}</option>`).join('')}</select></label><p class="creative-picker-intent" data-first-picker-intent></p><div class="creative-picker-tabs" role="group" aria-label="初稿图片来源">${[['recommended', '按本段推荐'], ['library', '浏览创意库'], ['project', '项目图片']].map(([key,label]) => `<button type="button" class="text-button" data-first-picker-source="${key}" aria-pressed="${key === picker.source}">${label}</button>`).join('')}</div><form class="creative-picker-search"><label><span class="sr-only">搜索图片内容</span><input type="search" data-first-picker-query placeholder="搜索图片内容" maxlength="300"></label><button class="secondary-button compact" type="submit">搜索</button><button class="text-button" type="button" data-first-picker-reset>清除筛选</button></form><div class="creative-picker-filter-host"></div><div class="creative-picker-results"></div><p class="first-picker-summary" data-first-picker-selection-count role="status"></p><div class="first-picker-assignments"></div><footer><button type="button" class="text-button" data-first-picker-upload>上传图片</button><span data-first-picker-status role="status">${picker.revision !== view.data.revision ? '脚本已更新，原配图草稿保留。请读取最新依据后核对分配。' : '选图后核对分配，再保存到段落。'}</span><button type="button" class="text-button" data-first-picker-rebase${picker.revision === view.data.revision ? ' hidden' : ''}>读取最新依据</button><button type="button" class="primary-button compact" data-first-picker-save>保存段落配图</button></footer></section>`;
  document.body.appendChild(panel); document.body.classList.add('creative-picker-open'); firstCreativePicker = picker;
  panel.addEventListener('click', async event => {
    if (event.target === panel || event.target.closest('[data-first-picker-close]')) { firstPickerClose(); return; }
    if (picker.busy) return;
    if (event.target.closest('[data-first-picker-rebase]')) {
      if (picker.pending) return;
      if (!await refreshFirstCreative(view) || !firstPickerActive(picker)) return;
      picker.revision = view.data.revision; picker.projectVersion = view.data.project_version; picker.needsRebase = false;
      const currentBlocks = new Set(view.data.blocks.map(block => block.block_id));
      const originalTargets = new Set(picker.touched);
      picker.touched = new Set([...picker.touched].filter(id => currentBlocks.has(id)));
      picker.selections = [
        ...picker.selections.filter(item => originalTargets.has(item.block_id) || !currentBlocks.has(item.block_id)),
        ...(view.data.image_references || []).filter(item => !picker.touched.has(item.block_id)),
      ];
      panel.querySelector('[data-first-picker-status]').textContent = '已读取最新依据，原选择保留。请核对目标段落后保存。';
      renderFirstPickerAssignments(picker); await loadFirstPicker(picker); return;
    }
    if (picker.pending && event.target.closest('[data-first-picker-remove],[data-first-picker-candidate]')) { panel.querySelector('[data-first-picker-status]').textContent = '上次保存结果尚未确定，请先点击读取原保存结果 / 重试，恢复后再修改分配。'; return; }
    const source = event.target.closest('[data-first-picker-source]');
    if (source) { picker.source = source.dataset.firstPickerSource; picker.page = 1; await loadFirstPicker(picker); return; }
    const page = event.target.closest('[data-first-picker-page]');
    if (page) { picker.page = Number(page.dataset.firstPickerPage); await loadFirstPicker(picker); return; }
    if (event.target.closest('[data-first-picker-reload]')) { await loadFirstPicker(picker); return; }
    if (event.target.closest('[data-first-picker-reset]')) { resetCreativePickerFilters(picker); panel.querySelector('[data-first-picker-query]').value = ''; await loadFirstPicker(picker); return; }
    const remove = event.target.closest('[data-first-picker-remove]');
    if (remove) { const [item] = picker.selections.splice(Number(remove.dataset.firstPickerRemove), 1); if (view.data.blocks.some(block => block.block_id === item.block_id)) picker.touched.add(item.block_id); firstPickerNormalizePrimary(picker, item.block_id); renderFirstPickerAssignments(picker); return; }
    const retry = event.target.closest('[data-first-picker-preview-retry]');
    if (retry) { const index = Number(retry.dataset.firstPickerPreviewRetry), host = panel.querySelector(`[data-first-picker-preview="${index}"]`); try { const url = await firstPickerPreview(picker, picker.candidates[index]); host.innerHTML = `<img src="${escapeHtml(url)}" alt="图片预览">`; panel.querySelector(`[data-first-picker-candidate="${index}"]`).disabled = false; panel.querySelector(`[data-first-picker-preview-status="${index}"]`).replaceChildren(); } catch(error) { host.innerHTML = formalPreviewFailureMarkup(error, `data-first-picker-preview-retry="${index}"`); } return; }
    const candidate = event.target.closest('[data-first-picker-candidate]');
    if (candidate) {
      const item = picker.candidates[Number(candidate.dataset.firstPickerCandidate)];
      const existing = picker.selections.findIndex(entry => entry.block_id === picker.blockId && firstPickerIdentity(entry) === firstPickerIdentity(item));
      if (existing >= 0) picker.selections.splice(existing, 1);
      else { picker.selections.push({...item, block_id: picker.blockId, primary: !picker.selections.some(entry => entry.block_id === picker.blockId)}); }
      picker.touched.add(picker.blockId); firstPickerNormalizePrimary(picker, picker.blockId); renderFirstPickerAssignments(picker); return;
    }
    if (event.target.closest('[data-first-picker-save]')) await saveFirstPicker(picker);
    if (event.target.closest('[data-first-picker-upload]')) {
      firstPickerClose();
      await navigateFormal(formalPath('materials', picker.projectId, {block_id: picker.blockId, creative: '1', return_to: 'creative-images'}));
      elements.formalImageUploadFiles.click();
    }
  });
  panel.addEventListener('change', async event => {
    if (picker.busy) return;
    const target = event.target.closest('[data-first-picker-target]');
    if (target) { picker.blockId = target.value; picker.page = 1; renderFirstPickerAssignments(picker); await loadFirstPicker(picker); return; }
    if (picker.pending && event.target.closest('[data-first-picker-assign],[data-first-picker-primary]')) { panel.querySelector('[data-first-picker-status]').textContent = '请先恢复上次保存结果，再修改图片分配。'; renderFirstPickerAssignments(picker); return; }
    const assign = event.target.closest('[data-first-picker-assign]');
    if (assign) {
      const item = picker.selections[Number(assign.dataset.firstPickerAssign)], old = item.block_id;
      item.block_id = assign.value; item.primary = !picker.selections.some(entry => entry !== item && entry.block_id === item.block_id);
      if (view.data.blocks.some(block => block.block_id === old)) picker.touched.add(old);
      picker.touched.add(item.block_id); firstPickerNormalizePrimary(picker, old); renderFirstPickerAssignments(picker); return;
    }
    const primary = event.target.closest('[data-first-picker-primary]');
    if (primary) { const item = picker.selections[Number(primary.dataset.firstPickerPrimary)]; picker.selections.filter(entry => entry.block_id === item.block_id).forEach(entry => { entry.primary = entry === item; }); picker.touched.add(item.block_id); renderFirstPickerAssignments(picker); return; }
    const facet = event.target.closest('[data-first-picker-filter]'), scope = event.target.closest('[data-first-picker-match-scope]');
    if (facet) picker.filters[facet.dataset.firstPickerFilter] = facet.value;
    if (scope) picker.matchScope = scope.value;
    if (facet || scope) { picker.page = 1; await loadFirstPicker(picker); }
  });
  panel.querySelector('form').addEventListener('submit', event => { event.preventDefault(); picker.query = panel.querySelector('[data-first-picker-query]').value.trim(); picker.page = 1; loadFirstPicker(picker); });
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); firstPickerClose(); return; }
    if (event.key !== 'Tab') return;
    const controls = [...panel.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),summary')].filter(item => item.getClientRects().length);
    if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); }
    else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); }
  });
  panel.querySelector('[data-first-picker-close]').focus({preventScroll: true});
  renderFirstPickerAssignments(picker); await loadFirstPicker(picker);
}
document.querySelector('#creative-first-draft').addEventListener('click', event => {
  const button = event.target.closest('[data-first-pick-image]');
  if (button) void openFirstCreativeImagePicker(button.dataset.firstPickImage);
});

let creativeImagePicker = null;
function creativePickerActive(picker) {
  return creativeImagePicker === picker && formalRoute().projectId === picker.projectId
    && state.formalImageWorkspace?.base_version_id === picker.baseVersionId;
}
function closeCreativeImagePicker({ restoreFocus = true, updateUrl = true } = {}) {
  const picker = creativeImagePicker;
  if (!picker) return;
  picker.listController?.abort();
  formalPreviewQueue?.release(picker.previewOwner);
  formalPreviewQueue?.release(picker.detailOwner);
  creativeImagePicker = null;
  document.getElementById("creative-image-picker")?.remove();
  document.body.classList.remove("creative-picker-open");
  if (updateUrl) { const url = new URL(window.location.href); url.searchParams.delete("picker"); window.history.replaceState({}, "", `${url.pathname}${url.search}`); }
  if (restoreFocus) (picker.opener?.isConnected ? picker.opener : elements.creativeCurrentImage.querySelector("button"))?.focus({ preventScroll: true });
}
async function openCreativeImagePicker(source = "current", shotId = creativeState().shotId, {batchTaskId = '', initialQuery = ''} = {}) {
  if (!await flushFormalScriptChanges()) return;
  captureFormalImageInputs();
  let shot = state.formalImageWorkspace?.shots?.find(item => item.shot_id === shotId);
  if (!shot) { await loadFormalShotImages(formalRoute().projectId); shot = state.formalImageWorkspace?.shots?.find(item => item.shot_id === shotId); if (!shot) return; }
  if (source === "current") source = shot.library_candidate ? "library" : "project";
  closeCreativeImagePicker({ restoreFocus: false, updateUrl: false });
  const picker = { projectId: formalRoute().projectId, baseVersionId: state.formalImageWorkspace.base_version_id,
    projectVersion: state.formalImageWorkspace.project_version, shotId, shotNo: shot.shot_no, source,
    query: initialQuery, page: 1, matchScope: "all", filters: {}, candidates: [], selected: source === "project" ? formalImageSelectedCandidate(shot) : shot.library_candidate,
    opener: document.activeElement, request: 0, busy: false, proposal: null, batchTaskId };
  if (picker.selected?.image_key) picker.selectedContext = { query: shot.query || "", digest: shot.candidate_snapshot_digest, candidates: [...(shot.candidates || [])] };
  creativeImagePicker = picker;
  const url = new URL(window.location.href); url.searchParams.set("picker", "images");
  window.history.pushState({}, "", `${url.pathname}${url.search}`);
  document.body.classList.add("creative-picker-open");
  document.body.insertAdjacentHTML("beforeend", `<div id="creative-image-picker" class="creative-picker-backdrop"><section class="creative-picker" role="dialog" aria-modal="true" aria-labelledby="creative-picker-title"><header><div><span>为当前镜头选图</span><h2 id="creative-picker-title">第 ${escapeHtml(shot.shot_no)} 镜</h2></div><button type="button" class="secondary-button compact" data-picker-close aria-label="关闭选图">关闭</button></header><p class="creative-picker-intent" title="${escapeHtml(creativeShotVisual(state.formalScript?.shots?.find(item => item.shot_id === shotId)) || shot.visual)}">${escapeHtml(creativeShotVisual(state.formalScript?.shots?.find(item => item.shot_id === shotId)) || shot.visual)}</p><p class="creative-picker-basis">推荐依据当前已保存工作稿。查看真实图片，点击用于本镜后保存选图进度。</p><div class="creative-picker-tabs" role="group" aria-label="图片来源">${[["project", "项目图片"], ["library", "创意库"], ["recommended", "智能推荐"]].map(([key, label]) => `<button type="button" class="text-button" data-picker-source="${key}" aria-pressed="${key === source}">${label}</button>`).join("")}</div><form class="creative-picker-search"><label><span class="visually-hidden">搜索图片</span><input type="search" placeholder="搜索图片内容" maxlength="300" data-picker-query></label><button type="submit" class="secondary-button compact">搜索</button><button type="button" class="text-button" data-picker-reset>清除筛选</button></form><div class="creative-picker-filter-host"></div><div class="creative-picker-results" tabindex="-1"><p role="status">正在读取图片…</p></div><div class="creative-picker-detail"></div><footer><button type="button" class="text-button" data-picker-upload>上传图片</button><span data-picker-status role="status"></span><button type="button" class="primary-button compact" data-picker-use disabled>用于本镜</button><button type="button" class="secondary-button compact" data-picker-adopt hidden>预览正式采用</button></footer></section></div>`);
  const panel = document.getElementById("creative-image-picker");
  panel.querySelector("[data-picker-query]").value = initialQuery;
  if (batchTaskId) {
    panel.querySelector('.creative-picker-basis').textContent = '本次选择只更新批量建议。返回后点击“应用所选”，才会保存选图进度。';
    panel.insertAdjacentHTML('beforeend', '<input type="file" accept=".jpg,.jpeg,.png,.webp" data-picker-batch-upload hidden>');
    panel.querySelector('[data-picker-batch-upload]').addEventListener('change', async event => {
      const file = event.target.files?.[0]; if (!file || picker.busy) return;
      picker.busy = true;
      const form = new FormData(); form.append('file', file, file.name);
      try {
        const result = await api(`/api/formal/projects/${encodeURIComponent(picker.projectId)}/image-workspace/supplements?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${picker.projectVersion}`, {method:'POST', body:form});
        if (await reviseImageBatch(batchTaskId, [{shot_id: picker.shotId, image_key: result.image_key}])) closeCreativeImagePicker();
      } catch (error) { if (panel.isConnected) panel.querySelector('[data-picker-status]').textContent = formalError(error); }
      finally { picker.busy = false; }
    });
  }
  panel.addEventListener("click", async event => {
    if (event.target === panel || event.target.closest("[data-picker-close]")) { closeCreativeImagePicker(); return; }
    const tab = event.target.closest("[data-picker-source]");
    if (tab && !picker.busy) { picker.source = tab.dataset.pickerSource; picker.page = 1; picker.selected = null; picker.proposal = null; await loadCreativePicker(picker); }
    const candidate = event.target.closest("[data-picker-candidate]");
    if (candidate && !picker.busy) { picker.selected = picker.candidates[Number(candidate.dataset.pickerCandidate)]; picker.selectedContext = { query: picker.query, digest: picker.digest, candidates: [...picker.candidates] }; picker.proposal = null; void loadCreativePickerDetail(picker); }
    const retry = event.target.closest("[data-picker-retry]");
    if (retry) { void loadCreativePickerCard(picker, Number(retry.dataset.pickerRetry)); return; }
    if (event.target.closest("[data-picker-detail-retry]")) { void loadCreativePickerDetail(picker); return; }
    if (event.target.closest("[data-picker-reset]")) {
      resetCreativePickerFilters(picker); panel.querySelector("[data-picker-query]").value = "";
      await loadCreativePicker(picker); return;
    }
    const page = event.target.closest("[data-picker-page]");
    if (page && !picker.busy) { picker.page = Number(page.dataset.pickerPage); await loadCreativePicker(picker); }
    if (event.target.closest("[data-picker-use]")) await useCreativePicker(picker);
    if (event.target.closest("[data-picker-adopt]")) await adoptCreativePicker(picker);
    if (event.target.closest("[data-picker-upload]")) {
      if (picker.batchTaskId) { panel.querySelector('[data-picker-batch-upload]').click(); return; }
      const index = state.formalImageWorkspace.shots.findIndex(item => item.shot_id === picker.shotId);
      closeCreativeImagePicker(); supplementFormalImageShot(index);
    }
  });
  panel.querySelector("form").addEventListener("submit", event => { event.preventDefault(); picker.query = panel.querySelector("[data-picker-query]").value.trim(); picker.page = 1; loadCreativePicker(picker); });
  panel.addEventListener("change", event => {
    const scope = event.target.closest("[data-picker-match-scope]"), facet = event.target.closest("[data-picker-filter]");
    if (picker.busy || (!scope && !facet)) return;
    if (scope) picker.matchScope = scope.value;
    if (facet) picker.filters[facet.dataset.pickerFilter] = facet.value;
    picker.page = 1; loadCreativePicker(picker);
  });
  panel.addEventListener("toggle", event => {
    if (!event.target.matches?.("[data-picker-duplicates]") || !event.target.open) return;
    event.target.querySelectorAll("[data-picker-candidate]").forEach(button => void loadCreativePickerCard(picker, Number(button.dataset.pickerCandidate)));
  }, true);
  panel.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); closeCreativeImagePicker(); return; }
    if (event.key !== "Tab") return;
    const controls = [...panel.querySelectorAll("button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, a[href]")]
      .filter(item => !item.hidden && !item.closest("[hidden]") && item.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  panel.querySelector("[data-picker-close]").focus({ preventScroll: true });
  await loadCreativePicker(picker);
}
async function loadCreativePicker(picker) {
  if (!creativePickerActive(picker)) return;
  const request = ++picker.request;
  picker.listController?.abort();
  picker.listController = typeof AbortController === "function" ? new AbortController() : null;
  formalPreviewQueue?.release(picker.previewOwner);
  if (!picker.selected) formalPreviewQueue?.release(picker.detailOwner);
  picker.previewOwner = `picker:${picker.projectId}:${picker.baseVersionId}:${request}`;
  const panel = document.getElementById("creative-image-picker"), results = panel.querySelector(".creative-picker-results");
  results.setAttribute("aria-busy", "true");
  panel.querySelectorAll("[data-picker-source]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.pickerSource === picker.source)));
  try {
    let model;
    if (picker.source === "project") {
      model = await api(formalImageCandidateSearchPath(picker.projectId, state.formalImageWorkspace, picker.shotNo, picker.query), { signal: picker.listController?.signal });
      if (!creativePickerActive(picker) || request !== picker.request) return;
      picker.candidates = model.candidates || []; picker.digest = model.candidate_snapshot_digest;
    } else {
      model = await api(creativePickerCandidatesPath(picker), { signal: picker.listController?.signal });
      if (!creativePickerActive(picker) || request !== picker.request) return;
      picker.candidates = picker.source === "library" ? model.library_candidates || []
        : model.candidates || (model.shots || []).find(item => item.shot_id === picker.shotId || String(item.shot_no) === String(picker.shotNo))?.candidates || [];
    }
    syncFormalPreviewModel(picker.projectId, picker.projectVersion, model);
    picker.model = model;
    const host = panel.querySelector(".creative-picker-filter-host");
    const expanded = Boolean(host.querySelector("details")?.open);
    const focusedFacet = host.contains?.(document.activeElement) ? document.activeElement.dataset.pickerFilter : null;
    const focusedScope = host.contains?.(document.activeElement) && document.activeElement.hasAttribute("data-picker-match-scope");
    const filterScroll = host.querySelector(".creative-picker-filters")?.scrollTop || 0;
    host.innerHTML = picker.source === "project" ? "" : creativeLibraryFiltersMarkup(model, picker.filters, {
      matchScope: picker.matchScope, hasShot: Boolean(picker.shotNo), recommended: picker.source === "recommended", expanded,
    });
    const filters = host.querySelector(".creative-picker-filters");
    if (filters) filters.scrollTop = filterScroll;
    const focusTarget = focusedFacet ? host.querySelector(`[data-picker-filter="${focusedFacet}"]`)
      : focusedScope ? host.querySelector("[data-picker-match-scope]") : null;
    focusTarget?.focus({ preventScroll: true });
    results.innerHTML = renderCreativePickerResults(picker, model);
    renderCreativePickerDetail(picker);
    if (picker.selected) void loadCreativePickerDetail(picker);
    const groups = creativePickerCandidateGroups(picker.candidates);
    const visible = groups.flatMap(values => values.slice(1).some(item => creativePickerImageKey(item) === creativePickerImageKey(picker.selected)) ? values : values.slice(0, 1));
    await Promise.allSettled(visible.map(item => loadCreativePickerCard(picker, picker.candidates.indexOf(item))));
  } catch (error) {
    if (error?.name !== "AbortError" && creativePickerActive(picker) && request === picker.request) {
      results.innerHTML = `<p role="alert">${escapeHtml(formalError(error))}。可切换来源或再次搜索。</p><button type="button" class="secondary-button compact" data-picker-page="${picker.page}">重新读取图库</button>`;
    }
  } finally { if (results.isConnected && request === picker.request) results.removeAttribute("aria-busy"); }
}

async function loadCreativePickerCard(picker, index) {
  const item = picker.candidates[index], request = picker.request;
  if (!item || !creativePickerActive(picker)) return;
  const panel = document.getElementById("creative-image-picker"), results = panel.querySelector(".creative-picker-results");
  const target = results.querySelector(`[data-picker-pixel="${index}"]`), status = results.querySelector(`[data-picker-card-status="${index}"]`);
  const active = () => creativePickerActive(picker) && request === picker.request && picker.candidates[index] === item && target?.isConnected;
  if (!target) return;
  const updatePreviewProgress = value => {
    target.dataset.pickerPreviewState = value;
    const progress = results.querySelector("[data-picker-preview-progress]");
    if (!progress) return;
    const count = previewState => results.querySelectorAll(`[data-picker-preview-state="${previewState}"]`).length;
    progress.textContent = `缩略图：已加载 ${count("loaded")} · 加载中 ${count("loading")} · 失败 ${count("failed")}`;
  };
  updatePreviewProgress("loading");
  if (status) status.textContent = "";
  try {
    const url = await requestFormalPreview(creativePickerPreviewPath(picker, item), {
      candidate: item, model: picker.model, variant: "thumbnail", owner: picker.previewOwner,
      priority: creativePickerImageKey(item) === creativePickerImageKey(picker.selected) ? 0 : 1,
    });
    if (!active()) return;
    state.formalImageThumbnailUrls.set(creativePickerImageKey(item), url);
    const image = document.createElement("img");
    image.src = url; image.alt = item.actual_caption || item.caption || "图片缩略图"; image.decoding = "async";
    if (typeof image.decode === "function") await image.decode();
    if (!active()) return;
    image.classList?.add("is-loaded"); target.replaceChildren(image);
    if (item.is_animated) target.insertAdjacentHTML("beforeend", '<span class="creative-picker-animation">动图 · 原图查看动态</span>');
    updatePreviewProgress("loaded");
  } catch (error) {
    if (!active() || error?.name === "AbortError") return;
    target.textContent = error?.code === "PREVIEW_TIMEOUT" ? "缩略图加载超时" : "缩略图暂不可用";
    if (status) status.innerHTML = formalPreviewFailureMarkup(error, `data-picker-retry="${index}"`);
    updatePreviewProgress("failed");
  }
}

async function loadCreativePickerDetail(picker) {
  if (!creativePickerActive(picker) || !picker.selected) return;
  const item = picker.selected, key = creativePickerImageKey(item);
  const detailKey = formalPreviewCacheKey(creativePickerPreviewPath(picker, item), { candidate: item, model: picker.model });
  if (picker.detailKey === detailKey && (picker.detailLoading || !picker.detailError && state.formalImagePreviewUrls.has(key))) {
    renderCreativePickerDetail(picker); return;
  }
  formalPreviewQueue?.release(picker.detailOwner);
  picker.detailKey = detailKey;
  picker.detailRequest = (picker.detailRequest || 0) + 1;
  const owner = picker.detailOwner = `picker-detail:${picker.projectId}:${picker.detailRequest}`;
  picker.detailError = ""; picker.detailLoading = true;
  renderCreativePickerDetail(picker);
  const active = () => creativePickerActive(picker) && picker.selected === item && picker.detailOwner === owner;
  try {
    const url = await requestFormalPreview(creativePickerPreviewPath(picker, item), {
      candidate: item, model: picker.model, variant: "original", owner, priority: 0,
    });
    if (!active()) return;
    state.formalImagePreviewUrls.set(key, url);
  } catch (error) {
    if (active() && error?.name !== "AbortError") picker.detailError = error?.code === "PREVIEW_TIMEOUT" ? "原图加载超时，请重试" : "原图暂不可用，请重试";
  } finally {
    if (active()) { picker.detailLoading = false; renderCreativePickerDetail(picker); }
  }
}

function renderCreativePickerDetail(picker) {
  if (!creativePickerActive(picker)) return;
  const panel = document.getElementById("creative-image-picker"), item = picker.selected;
  const key = creativePickerImageKey(item), original = key && state.formalImagePreviewUrls.get(key);
  const thumbnail = key && state.formalImageThumbnailUrls.get(key), url = original || thumbnail;
  const outsideResults = item && !picker.candidates.some(candidate => creativePickerImageKey(candidate) === key);
  const status = picker.detailLoading ? "正在读取原图…" : picker.detailError || (original
    ? (outsideResults ? "当前暂选不在本次筛选结果中，仍可采用。请核对画面后选用。" : "原图已加载，请核对画面后选用。")
    : "点击图片可查看原图。" );
  panel.querySelector(".creative-picker-detail").innerHTML = item ? `<div class="creative-picker-selected">${url ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(item.actual_caption || item.caption || "所选图片预览")}" decoding="async">` : "<span>正在读取原图…</span>"}<div><strong>${escapeHtml(item.actual_caption || item.caption || "图片内容待识别")}</strong><p class="creative-preview-status" role="status">${escapeHtml(status)}</p>${picker.detailError ? '<button type="button" class="secondary-button compact" data-picker-detail-retry>重试原图</button>' : ""}<p>${escapeHtml(item.template_usage || "请结合当前镜头核对画面用途")}</p>${item.image_key ? formalImageCandidateEvidence(item) : formalDefaultCandidateDetails(item)}</div></div>` : '';
  panel.querySelectorAll("[data-picker-candidate]").forEach(button => {
    const selected = creativePickerImageKey(picker.candidates[Number(button.dataset.pickerCandidate)]) === key;
    button.setAttribute("aria-pressed", String(selected)); button.classList.toggle("is-selected", selected);
  });
  panel.querySelector("[data-picker-use]").disabled = picker.busy || !original || picker.detailLoading || Boolean(picker.detailError);
  panel.querySelector("[data-picker-use]").textContent = picker.batchTaskId ? '更新本镜建议' : state.formalScript?.can_save_version || formalScriptHasPendingWrites() ? "暂选并保存进度" : "用于本镜";
  const adopt = panel.querySelector("[data-picker-adopt]");
  adopt.hidden = Boolean(picker.batchTaskId) || !item?.candidate_key;
  adopt.disabled = picker.busy || !original || picker.detailLoading || Boolean(picker.detailError);
  adopt.textContent = picker.proposal ? "确认采用并返回本镜" : "预览正式采用";
}
async function useCreativePicker(picker, { close = true } = {}) {
  if (!creativePickerActive(picker) || picker.busy || !picker.selected) return false;
  const item = picker.selected, key = item.image_key || `default:${item.candidate_key}`;
  if (!state.formalImagePreviewUrls.has(key)) return false;
  await requestFormalPreview(creativePickerPreviewPath(picker, item), { candidate: item, model: picker.model,
    owner: `formal:${state.formalImageEpoch}`, priority: 0 });
  if (!creativePickerActive(picker) || picker.selected !== item) return false;
  if (picker.batchTaskId) {
    picker.busy = true; renderCreativePickerDetail(picker);
    const saved = await reviseImageBatch(picker.batchTaskId, [{shot_id: picker.shotId,
      ...(item.image_key ? {image_key: item.image_key} : {library_candidate_key: item.candidate_key}), query: item.image_key ? picker.selectedContext?.query ?? picker.query : picker.query}]);
    picker.busy = false;
    if (saved && close) closeCreativeImagePicker();
    else renderCreativePickerDetail(picker);
    return saved;
  }
  const shot = state.formalImageWorkspace.shots.find(item => item.shot_id === picker.shotId);
  if (!shot) return false;
  captureFormalImageInputs();
  if (item.image_key) {
    const context = picker.selectedContext;
    if (!context?.candidates.some(candidate => candidate.image_key === item.image_key)) { showToast("候选依据已变化，请重新点选图片后使用。"); return false; }
    shot.candidates = [...context.candidates]; shot.candidate_snapshot_digest = context.digest;
  }
  shot.selected_image_key = item.image_key || ""; shot.library_candidate_key = item.candidate_key || "";
  shot.library_candidate = item.candidate_key ? item : null;
  shot.decision_kind = "image_confirmed"; shot.confirmed = true; shot.progress_unavailable = false; shot.needs_recheck = false;
  shot.query = item.image_key ? picker.selectedContext.query : picker.query; shot.query_input = shot.query;
  markCreativeImageChange(shot); invalidateFormalImageVersionProposal();
  renderFormalImageWorkspace(state.formalImageWorkspace);
  const saved = await flushCreativeProgress({ manual: true });
  if (saved && close) closeCreativeImagePicker();
  else if (creativePickerActive(picker)) document.querySelector("[data-picker-status]").textContent = saved ? "暂选进度已保存" : "输入已保留，请重试保存进度";
  return saved;
}
async function adoptCreativePicker(picker) {
  if (!creativePickerActive(picker) || picker.busy || !picker.selected?.candidate_key || creativeImageDraftGate()) return;
  const panel = document.getElementById("creative-image-picker");
  if (!picker.proposal && !await useCreativePicker(picker, { close: false })) return;
  picker.busy = true; renderCreativePickerDetail(picker);
  try {
    if (!picker.proposal) {
      picker.proposal = await api(`/api/formal/projects/${encodeURIComponent(picker.projectId)}/images/default-proposals?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, { method: "POST", body: JSON.stringify({
        expected_project_version: picker.projectVersion, expected_script_revision: state.formalScript?.revision ?? picker.model?.script_revision ?? 0, allowed_shot_ids: [picker.shotId],
        selections: [{ shot_no: picker.shotNo, candidate_key: picker.selected.candidate_key }], idempotency_key: formalRequestKey("inline-image-adoption") }) });
      if (creativePickerActive(picker)) panel.querySelector("[data-picker-status]").textContent = picker.proposal.response?.summary || "采用后加入项目图片池，配图版本仍需确认保存。";
    } else {
      const response = await api(`/api/agent/tasks/${encodeURIComponent(picker.proposal.task_id)}/confirm`, { method: "POST", body: JSON.stringify({ proposal_digest: picker.proposal.response.proposal_digest }) });
      requireCompletedAgentAction(response);
      if (!creativePickerActive(picker)) return;
      const adopted = response.response?.adopted_selections?.find(item => item.shot_id === picker.shotId);
      if (!adopted) throw new Error("采用已完成，但返回图片未能定位当前镜头。请重新读取项目图片。 ");
      const projectId = picker.projectId, shotId = picker.shotId;
      if (Number.isInteger(response.response.project_version) && state.formalScript) state.formalScript.project_version = response.response.project_version;
      await loadFormalShotImages(projectId);
      const shot = state.formalImageWorkspace?.shots?.find(item => item.shot_id === shotId);
      if (!shot || formalRoute().projectId !== projectId) return;
      const imageWorkspace = state.formalImageWorkspace;
      const model = await api(formalImageCandidateSearchPath(projectId, imageWorkspace, shot.shot_no, ""));
      if (state.formalImageWorkspace !== imageWorkspace || formalRoute().projectId !== projectId) return;
      shot.candidates = model.candidates || []; shot.candidate_snapshot_digest = model.candidate_snapshot_digest;
      const selected = shot.candidates.find(item => item.image_key === adopted.image_key && item.content_sha256 === adopted.content_sha256);
      if (!selected) { showToast("图片已加入项目池，请查找并核对本镜图片。"); return; }
      shot.selected_image_key = selected.image_key; shot.library_candidate_key = ""; shot.library_candidate = null;
      shot.confirmed = false; shot.query = ""; shot.query_input = ""; markCreativeImageChange(shot);
      renderFormalImageWorkspace(state.formalImageWorkspace); await loadFormalShotCandidatePreview(state.formalImageWorkspace.shots.indexOf(shot));
      // Replace the temporary library selection already queued before adoption with the adopted project image.
      captureFormalImageInputs();
      await flushCreativeProgress({ manual: true });
      creativeState().imageKey = `${state.formalScript?.base_version_id || imageWorkspace.base_version_id}:${state.formalScript?.project_version || imageWorkspace.project_version}`;
      closeCreativeImagePicker(); selectCreativeShot(shotId); showToast("图片已采用。核对本镜后保存配图版本。");
    }
  } catch (error) { if (panel.isConnected) panel.querySelector("[data-picker-status]").textContent = formalError(error); }
  finally { picker.busy = false; if (creativePickerActive(picker)) renderCreativePickerDetail(picker); }
}

function formalDefaultImageOptions(shot) {
  return (shot.selected_candidate_key ? "" : '<option value="" selected>请选择图片</option>') + formalVisibleImageCandidates(shot.candidates || [], "candidate_key", [shot.selected_candidate_key], Boolean(shot.show_duplicate_sources)).map((item) => `<option value="${escapeHtml(item.candidate_key)}"${item.candidate_key === shot.selected_candidate_key ? " selected" : ""}>${escapeHtml(item.caption)}${item.duplicate_count ? ` · 同图 ${Number(item.duplicate_count) + 1} 个来源` : ""}</option>`).join("");
}

function formalImageSelectedCandidate(shot) {
  return (shot?.candidates || []).find((item) => item.image_key === shot.selected_image_key) || null;
}

function formalImageDisplay(shot) {
  const selected = shot && formalImageSelectedCandidate(shot);
  if (shot?.library_candidate_key) return { candidate: shot.library_candidate, url: state.formalImagePreviewUrls.get(`default:${shot.library_candidate_key}`) || "", label: "创意库暂选，待正式采用" };
  if (shot?.explicitly_changed) return {
    candidate: selected, url: selected ? state.formalImagePreviewUrls.get(selected.image_key) || "" : "",
    label: shot.decision_kind === "temporary_text_basis" ? "暂准文字依据，待补图" : "已选用，待保存配图版本",
  };
  const current = shot?.current_image;
  if (current) return {candidate: current, url: state.formalImagePreviewUrls.get(current.image_key) || "", label: "当前确认图"};
  const reference = shot?.reference_image;
  if (reference) return {candidate: reference, url: /^data:image\/(png|jpeg|gif|bmp|webp);base64,/.test(reference.inline_preview || "") ? reference.inline_preview : "", label: "版本参考图，待确认"};
  // Older server DTOs did not distinguish a frozen reference from a suggestion.
  if (shot && !Object.prototype.hasOwnProperty.call(shot, "reference_image")) return {
    candidate: selected, url: selected ? state.formalImagePreviewUrls.get(selected.image_key) || "" : "",
    label: shot.confirmed ? "当前确认图" : "推荐候选，待确认",
  };
  return {candidate: null, url: "", label: "本版本暂无参考图"};
}

function formalImageCandidateHasRealPreview(candidate) {
  if (!candidate) return false;
  if (candidate.preview_available === false || candidate.has_preview === false) return false;
  if (["unavailable", "missing", "failed", "blocked"].includes(String(candidate.preview_status || "").toLowerCase())) return false;
  if (Object.prototype.hasOwnProperty.call(candidate, "preview_url") && !candidate.preview_url) return false;
  return true;
}

function formalImageExplanationValue(value) {
  if (Array.isArray(value)) return value.map((item) => formalImageExplanationValue(item)).filter(Boolean).join("、");
  if (value && typeof value === "object") return value.label || value.summary || value.reason || "";
  return value === undefined || value === null ? "" : String(value);
}

function formalImageMatchExplanation(candidate) {
  const explanation = candidate?.match_explanation;
  if (!explanation || typeof explanation !== "object") return "";
  const rows = [
    ["综合评分", explanation.total_score],
    ["推荐结论", explanation.recommendation],
    ["语义匹配", explanation.semantic],
    ["安全检查", explanation.safety],
    ["命中依据", explanation.hits],
    ["缺失信息", explanation.missing],
    ["镜头功能", explanation.shot_functions],
    ["冲突", explanation.conflicts],
    ["复用提示", explanation.reuse],
    ["近似提示", explanation.near_duplicate],
  ].map((item) => [item[0], formalImageExplanationValue(item[1])]).filter((item) => item[1]);
  if (!rows.length) return "";
  return `<details class="ui00-image-match-explanation"><summary>查看完整匹配解释</summary><dl>${rows.map((item) => `<div><dt>${escapeHtml(item[0])}</dt><dd>${escapeHtml(item[1])}</dd></div>`).join("")}</dl></details>`;
}

function formalImageFailureSummary(shot) {
  const failure = shot?.failure_summary || shot?.gap_reason;
  if (!failure) return "";
  const title = (shot.candidates || []).length ? "候选需人工核对" : "暂无匹配图片";
  const message = typeof failure === "object" ? failure.message || failure.summary || failure.reason || "" : String(failure);
  const actions = typeof failure === "object" && Array.isArray(failure.next_steps) ? failure.next_steps : [];
  const notice = `<aside class="ui00-image-gap" role="status"><strong>${escapeHtml(title)}</strong>${message ? `<p>${escapeHtml(message)}</p>` : ""}${actions.length ? `<ul>${actions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}</aside>`;
  return shot.confirmed && !shot.explicitly_changed
    ? `<details class="candidate-only-notice"><summary>其他候选的匹配提示</summary><p>当前配图已保存，以下为候选检索提示。</p>${notice}</details>` : notice;
}

function formalImageCandidateEvidence(candidate) {
  if (!candidate) return "";
  const source = candidate.sources?.[0];
  const conflicts = (candidate.conflicts || []).join("、") || candidate.brand_warning || "未提供冲突记录";
  const quality = candidate.quality_evidence || {};
  const qualityFlags = (quality.quality_flags || []).join("、") || "未提供质量警告记录";
  const dimensions = quality.pixel_width && quality.pixel_height ? `${quality.pixel_width}×${quality.pixel_height}` : "尺寸待核对";
  return `<details class="ui00-image-evidence-stack"><summary>匹配依据与来源</summary><dl class="ui00-image-candidate-evidence"><div><dt>匹配依据</dt><dd>${escapeHtml(candidate.match_basis || "当前未提供匹配说明")}</dd></div><div><dt>质量</dt><dd>${escapeHtml(`${quality.summary || "质量说明待补充"} · ${dimensions} · ${qualityFlags}`)}</dd></div><div><dt>冲突</dt><dd>${escapeHtml(conflicts)}</dd></div><div><dt>来源</dt><dd>${escapeHtml(source ? formalImageSourceLabel(source) : "来源待补充")}</dd></div><div><dt>当前版本复用</dt><dd>${escapeHtml(String(candidate.prospective_reuse_count || 0))} 镜</dd></div></dl>${formalImageMatchExplanation(candidate)}</details>`;
}

function refreshFormalImageReuseState() {
  const shots = state.formalImageWorkspace?.shots || [];
  const counts = new Map();
  shots.forEach((shot) => {
    if (shot.decision_kind === "temporary_text_basis") return;
    const candidate = formalImageSelectedCandidate(shot);
    const cluster = candidate?.perceptual_cluster_id || candidate?.image_key;
    if (cluster) counts.set(cluster, (counts.get(cluster) || 0) + 1);
  });
  shots.forEach((shot) => {
    const selected = formalImageSelectedCandidate(shot);
    const selectedCluster = selected?.perceptual_cluster_id || selected?.image_key;
    shot.reuse_required = Boolean(selectedCluster && (counts.get(selectedCluster) || 0) > 1);
    (shot.candidates || []).forEach((candidate) => {
      const cluster = candidate.perceptual_cluster_id || candidate.image_key;
      candidate.prospective_reuse_count = cluster === selectedCluster
        ? (counts.get(cluster) || 0)
        : (counts.get(cluster) || 0) + 1;
    });
    const index = shots.indexOf(shot);
    const label = elements.formalImageShotGrid?.querySelector(`[data-image-reuse-reason="${index}"]`);
    if (label) label.hidden = (!shot.reuse_required && !formalImageReuseTargets(shot).length) || shot.decision_kind === "temporary_text_basis";
  });
}

function formalImageDecisionPreview(shot, index) {
  const {candidate: selected, url, label} = formalImageDisplay(shot);
  const source = selected?.sources?.[0];
  const fallback = shot.preview_loading ? "正在读取真实像素预览…" : shot.preview_error || "选择图片后显示预览";
  return `<div class="ui00-image-shot-preview" id="formal-image-shot-preview-${index}">${url ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(`第 ${shot.shot_no} 镜${label}`)}" width="1280" height="720" loading="lazy" decoding="async" />` : `<span>${escapeHtml(fallback)}</span>`}</div><p class="ui00-image-shot-source"><strong>${escapeHtml(label)}</strong>${escapeHtml(source ? ` · ${formalImageSourceLabel(source)}` : "")}</p>`;
}

function renderFormalImageWorkspace(value) {
  syncFormalPreviewModel(formalRoute().projectId, value?.project_version, value);
  state.formalImageWorkspace = value;
  refreshFormalImageReuseState();
  state.formalImageVersionProposal = null;
  elements.formalImageVersionProposal.hidden = true;
  elements.formalImageVersionProposal.replaceChildren();
  const shots = Array.isArray(value?.shots) ? value.shots : [];
  elements.formalImageVersionState.textContent = value?.state === "confirmed" ? "已确认" : shots.length ? "待配图确认" : "等待脚本";
  elements.formalImageVersionMessage.hidden = ["ready", "confirmed"].includes(value?.state);
  elements.formalImageVersionMessage.textContent = value?.message || "请先完成脚本生成。";
  elements.formalImageVersionForm.hidden = !shots.length;
  elements.formalImageShotGrid.innerHTML = shots.map((shot, index) => {
    const options = formalImageShotOptions(shot);
    const selected = formalImageSelectedCandidate(shot);
    const temporary = shot.decision_kind === "temporary_text_basis";
    const imagePreviewReady = temporary || (formalImageCandidateHasRealPreview(selected) && state.formalImagePreviewUrls.has(shot.selected_image_key));
    const libraryHref = formalPath("materials", state.formalImageProjectId || formalRoute().projectId, { return_to: "shot-images", shot_id: shot.shot_id, focus_shot: shot.shot_no, pick: "library" });
    return `<article class="ui00-image-shot-card" data-image-shot="${index}" tabindex="-1"><header><div class="ui00-image-shot-heading"><span>镜头 ${escapeHtml(shot.shot_no)}</span><small data-image-shot-badge="${index}">${escapeHtml(formalImageDecisionIssue(shot) || "已确认")}</small></div><strong>${escapeHtml(shot.visual || "当前镜头")}</strong></header><div class="ui00-image-shot-visual">${formalImageDecisionPreview(shot, index)}${formalImageCandidateEvidence(selected)}${creativeReferenceMarkup(shot.shot_id)}</div><aside data-image-scope-notice hidden><p>当前仅查看此镜，未改变原配图范围。</p><button type="button" class="secondary-button compact" data-image-scope-current>将本镜设为修改范围</button></aside><fieldset class="ui00-image-shot-controls" data-image-edit-scope>${formalImageFailureSummary(shot)}<label for="formal-image-shot-select-${index}"><span>${(shot.candidates || []).length ? `候选图片 · ${shot.candidates.length} 张` : "暂无匹配图片"}</span><select id="formal-image-shot-select-${index}" data-image-shot-select="${index}"${temporary || !(shot.candidates || []).length ? " disabled" : ""}>${options}</select></label>${(shot.candidates || []).some((candidate) => candidate.duplicate_of) ? `<label class="ui00-duplicate-toggle"><input type="checkbox" data-image-shot-duplicates="${index}"${shot.show_duplicate_sources ? " checked" : ""} />显示同图的其他来源</label>` : ""}<div class="ui00-inline-actions"><button class="secondary-button compact" type="button" data-image-picker="${index}">选图与换图</button><button class="secondary-button compact" type="button" data-image-undo="${index}">撤销本镜配图</button><button class="secondary-button compact" type="button" data-image-undo-selected>撤销所选镜头配图</button><a class="text-button" href="${escapeHtml(libraryHref)}" data-image-library="1">创意库选图</a><button class="secondary-button compact" type="button" data-image-supplement="${index}">上传图片</button><button class="text-button" type="button" data-image-generate="${escapeHtml(shot.shot_id || "")}">生成新图</button></div><details class="ui00-image-search"><summary>按描述查找项目图片</summary><label for="formal-image-shot-query-${index}"><span class="sr-only">查找描述</span><span class="ui00-image-query-row"><input id="formal-image-shot-query-${index}" data-image-shot-query="${index}" maxlength="300" value="${escapeHtml(shot.query_input ?? shot.query ?? "")}" placeholder="例如：入口、人群、航拍" /><button class="secondary-button compact" type="button" data-image-shot-search="${index}">查找</button></span></label></details><div class="ui00-image-reuse" data-image-reuse-reason="${index}"${shot.reuse_required && !temporary ? "" : " hidden"}><label><span>为什么在这些镜头使用这张图</span><textarea data-image-shot-reuse-reason="${index}" maxlength="500" rows="2" placeholder="简述用途">${escapeHtml(shot.reuse_reason || "")}</textarea></label><button class="secondary-button compact" type="button" data-image-reuse-apply="${index}">用于这张图的其他镜头</button></div><details class="ui00-image-temporary-options"${temporary ? " open" : ""}><summary>暂时没有合适图片</summary><label class="ui00-image-shot-confirm"><input type="checkbox" data-image-shot-temporary="${index}"${temporary ? " checked" : ""} /><span>暂准文字依据</span></label><label data-image-temporary-reason="${index}"${temporary ? "" : " hidden"}><span>暂准理由 · 补图后才能审核交付</span><textarea data-image-shot-reason="${index}" maxlength="500" rows="2">${escapeHtml(shot.temporary_reason || "")}</textarea></label></details><input type="checkbox" data-image-shot-confirm="${index}"${imagePreviewReady ? "" : " disabled"}${shot.confirmed ? " checked" : ""} hidden aria-hidden="true" tabindex="-1" /><button class="primary-button ui00-image-use" type="button" data-image-shot-use="${index}">使用这张并继续</button></fieldset></article>`;
  }).join("");
  const jump = document.querySelector("#formal-image-jump");
  if (jump) jump.innerHTML = shots.map((shot, index) => `<option value="${index}">第 ${escapeHtml(shot.shot_no)} 镜</option>`).join("");
  elements.formalImageVersionHistory.innerHTML = (value?.history || []).map((item) => `<div class="ui00-image-version-item${item.is_current ? " is-current" : ""}"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.change_summary)}</span><small>${escapeHtml(item.created_at)}</small></div>`).join("");
  validateFormalImageSelections();
}

function updateFormalImageShotPreview(index) {
  const shot = state.formalImageWorkspace?.shots?.[index];
  const card = elements.formalImageShotGrid.querySelector(`[data-image-shot="${index}"]`);
  if (!shot || !card) return;
  const wrapper = document.createElement("div");
  wrapper.innerHTML = formalImageDecisionPreview(shot, index);
  card.querySelector(".ui00-image-shot-preview")?.replaceWith(wrapper.children[0]);
  card.querySelector(".ui00-image-shot-source")?.replaceWith(wrapper.children[0]);
  renderWorkbenchCards();
  const evidence = card.querySelector(".ui00-image-evidence-stack");
  if (evidence) {
    const evidenceWrapper = document.createElement("div");
    evidenceWrapper.innerHTML = formalImageCandidateEvidence(formalImageSelectedCandidate(shot));
    if (evidenceWrapper.firstElementChild) evidence.replaceWith(evidenceWrapper.firstElementChild);
    else evidence.remove();
  }
}

async function loadFormalShotCandidatePreview(index) {
  const projectId = formalRoute().projectId;
  const workspace = state.formalImageWorkspace;
  const epoch = state.formalImageEpoch;
  const active = () => state.formalImageWorkspace === workspace && state.formalImageEpoch === epoch && formalRoute().projectId === projectId;
  const shot = workspace?.shots?.[index];
  const imageKey = shot?.selected_image_key;
  if (!projectId || !shot || !imageKey || !formalImageCandidateHasRealPreview(formalImageSelectedCandidate(shot))) {
    validateFormalImageSelections();
    return;
  }
  shot.preview_loading = !state.formalImagePreviewUrls.has(imageKey);
  shot.preview_error = "";
  updateFormalImageShotPreview(index);
  validateFormalImageSelections();
  if (state.formalImagePreviewUrls.has(imageKey)) return;
  try {
    const path = `/api/formal/projects/${encodeURIComponent(projectId)}/images/${encodeURIComponent(imageKey)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(state.formalImageWorkspace.project_version)}`;
    const url = await requestFormalPreview(path, { candidate: formalImageSelectedCandidate(shot), model: workspace,
      owner: `formal:${epoch}`, priority: 0 });
    if (!active() || state.formalImageWorkspace?.shots?.[index]?.selected_image_key !== imageKey) return;
    state.formalImagePreviewUrls.set(imageKey, url);
  } catch (error) {
    if (active() && state.formalImageWorkspace?.shots?.[index]?.selected_image_key === imageKey) {
      shot.preview_error = formalError(error);
    }
  } finally {
    if (active() && state.formalImageWorkspace?.shots?.[index]?.selected_image_key === imageKey) {
      shot.preview_loading = false;
      updateFormalImageShotPreview(index);
      validateFormalImageSelections();
    }
  }
}

function validateFormalImageSelections() {
  captureFormalImageInputs();
  const shots = state.formalImageWorkspace?.shots || [];
  const changed = creativeChangedImageShots();
  refreshFormalImageReuseState();
  const unfinished = changed.find(shot => formalImageDecisionIssue(shot));
  const complete = (changed.length > 0 || state.formalScript?.can_save_version) && !state.formalImageVersionBusy;
  elements.formalImageVersionSave.disabled = !complete;
  const confirmed = changed.filter(shot => creativeImageStatus(shot) === "已确认待保存").length;
  const selected = shots.filter(shot => shot.confirmed || shot.explicitly_changed && (shot.selected_image_key || shot.library_candidate_key)).length;
  const progress = `全稿 ${shots.length} 镜 · 已选用 ${selected} 镜 · 未配图 ${shots.length - selected} 镜 · 已确认待保存 ${confirmed} 镜 · 本次修改 ${changed.length} 镜`;
  elements.formalImageSelectionStatus.textContent = !changed.length ? `${progress}。选择或确认图片后可保存。`
    : unfinished ? `${progress} · 第 ${unfinished.shot_no} 镜：${formalImageDecisionIssue(unfinished)}`
    : `${progress} · 可以查看统一保存预览`;
  const counter = document.querySelector("#formal-image-progress");
  if (counter) counter.textContent = progress;
  const next = document.querySelector("#formal-image-next-incomplete");
  if (next) next.disabled = nextImageNavigationIndex("pending") < 0;
  const gap = document.querySelector("#formal-image-next-gap");
  if (gap) gap.disabled = nextImageNavigationIndex("gap") < 0;
  shots.forEach((shot, index) => {
    const reuseButton = elements.formalImageShotGrid.querySelector(`[data-image-reuse-apply="${index}"]`);
    if (reuseButton) {
      const targets = formalImageReuseTargets(shot);
      reuseButton.textContent = `应用到另外 ${targets.length} 镜`;
      reuseButton.disabled = !targets.length || !String(shot.reuse_reason || "").trim() || state.formalImageVersionBusy;
    }
    const issue = formalImageDecisionIssue(shot);
    const checkbox = elements.formalImageShotGrid.querySelector(`[data-image-shot-confirm="${index}"]`);
    if (checkbox) checkbox.disabled = shot.decision_kind !== "temporary_text_basis" && (!formalImageCandidateHasRealPreview(formalImageSelectedCandidate(shot)) || !state.formalImagePreviewUrls.has(shot.selected_image_key));
    const badge = elements.formalImageShotGrid.querySelector(`[data-image-shot-badge="${index}"]`);
    if (badge) badge.textContent = shot.explicitly_changed ? issue || "本次已确认" : shot.confirmed ? "已保存配图" : "尚未选择";
    const use = elements.formalImageShotGrid.querySelector(`[data-image-shot-use="${index}"]`);
    if (use) {
      use.disabled = state.formalImageVersionBusy || Boolean(issue && issue !== "确认本镜" && !shot.library_candidate_key);
      use.textContent = shot.library_candidate_key ? "正式采用这张图" : shot.explicitly_changed && shot.confirmed ? "已选用" : shot.decision_kind === "temporary_text_basis" ? "确认本镜暂准文字" : "使用这张图片";
    }
  });
  syncCreativeShotVisibility();
  syncFormalScriptSaveControls(state.formalScript);
  syncCreativeProgressStatus();
}

async function loadFormalImageWorkspace(projectId, projectVersion, epoch = state.formalImageEpoch) {
  const active = () => state.formalImageEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "script";
  try {
    const value = await api(formalImageWorkspacePath(projectId, projectVersion));
    if (!active()) return;
    creativeState().imageBaseline = new Map((value.shots || []).map(shot => [shot.shot_id, { ...shot }]));
    await loadCreativeProgress(value, projectId);
    if (!active()) return;
    const pendingQueries = restoreFormalImageInputs(value, projectId);
    let unavailable = false;
    await Promise.all(pendingQueries.map(async ({ shot, prior, sameVersion }) => {
      try {
        const query = String(prior.query || "");
        const result = await api(formalImageCandidateSearchPath(projectId, value, shot.shot_no, query));
        if (!active()) return;
        shot.candidates = result.candidates || [];
        shot.candidate_snapshot_digest = result.candidate_snapshot_digest;
        shot.failure_summary = result.failure_summary || result.gap_reason || "";
        shot.query = query;
        const selected = shot.candidates.find((candidate) => candidate.image_key === prior.selected_image_key && formalImageCandidateHasRealPreview(candidate));
        shot.selected_image_key = selected?.image_key || "";
        shot.confirmed = Boolean(prior.confirmed && sameVersion && prior.candidate_snapshot_digest && prior.candidate_snapshot_digest === shot.candidate_snapshot_digest && (selected || shot.decision_kind === "temporary_text_basis"));
        if (!selected && prior.selected_image_key && shot.decision_kind !== "temporary_text_basis") unavailable = true;
      } catch (_error) {
        // Keep the fresh workspace response if a previous custom query cannot be restored.
        shot.confirmed = false;
        unavailable = true;
      }
    }));
    if (!active()) return;
    renderFormalImageWorkspace(value);
    queueCreativeProgress();
    syncCreativeNextAction();
    void restoreImageBatch(projectId);
    if (unavailable) showToast("部分原选图片不可用，请重新查找或选图。");
    const params = new URLSearchParams(window.location.search);
    const revisionTaskId = params.get("revision_task_id") || "";
    const revisionShot = params.get("revision_shot");
    if (revisionTaskId && revisionShot && state.formalImageRevisionTaskLoaded !== revisionTaskId) {
      const index = value.shots.findIndex((item) => String(item.shot_no) === revisionShot);
      if (index >= 0) {
        state.formalImageRevisionTaskLoaded = revisionTaskId;
        const query = elements.formalImageShotGrid.querySelector(`[data-image-shot-query="${index}"]`);
        if (query) query.value = "按历史图片反馈重新匹配，排除原图";
        const search = elements.formalImageShotGrid.querySelector(`[data-image-shot-search="${index}"]`);
        if (search) await searchFormalImageShot(search);
      }
    }
  } catch (error) {
    if (state.formalImageEpoch !== epoch || formalRoute().projectId !== projectId || formalRoute().page !== "script") return;
    renderFormalImageWorkspaceError("暂不可用", formalError(error));
  }
}

function formalImageCandidateSearchPath(projectId, workspace, shotNo, query) {
  const sourceTask = new URLSearchParams(window.location.search).get("revision_task_id") || "";
  return `/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/shots/${encodeURIComponent(shotNo)}/candidates?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&base_version_id=${encodeURIComponent(workspace.base_version_id)}&expected_project_version=${encodeURIComponent(workspace.project_version)}&query=${encodeURIComponent(query)}${sourceTask ? `&source_revision_task_id=${encodeURIComponent(sourceTask)}` : ""}`;
}

function changeFormalImageSelection(event) {
  const select = event.target.closest("[data-image-shot-select]");
  if (!select || !state.formalImageWorkspace) return;
  const index = Number(select.dataset.imageShotSelect);
  const shot = state.formalImageWorkspace.shots[index];
  const candidate = (shot.candidates || []).find((item) => item.image_key === select.value);
  if (!formalImageCandidateHasRealPreview(candidate)) {
    select.value = shot.selected_image_key || "";
    showToast("该候选没有真实像素预览，不能选择或确认。");
    return;
  }
  invalidateFormalImageVersionProposal();
  markCreativeImageChange(shot);
  shot.library_candidate_key = "";
  shot.library_candidate = null;
  shot.selected_image_key = select.value;
  shot.confirmed = false;
  shot.preview_error = "";
  refreshFormalImageReuseState();
  const checkbox = elements.formalImageShotGrid.querySelector(`[data-image-shot-confirm="${index}"]`);
  if (checkbox) checkbox.checked = false;
  state.formalImageWorkspace.shots.forEach((_item, shotIndex) => updateFormalImageShotPreview(shotIndex));
  validateFormalImageSelections();
  loadFormalShotCandidatePreview(index);
}

async function searchFormalImageShot(button) {
  const index = Number(button.dataset.imageShotSearch);
  const shot = state.formalImageWorkspace?.shots?.[index];
  if (!shot || state.formalImageVersionBusy) return;
  const query = elements.formalImageShotGrid.querySelector(`[data-image-shot-query="${index}"]`)?.value.trim() || "";
  await openCreativeImagePicker("project", shot.shot_id, { initialQuery: query });
}

async function undoCreativeImageChoices(shotIds) {
  const projectId = formalRoute().projectId;
  const workspace = state.formalImageWorkspace;
  const current = creativeState();
  const targets = new Set(shotIds);
  if (!projectId || !workspace || current.imageUndoBusy || state.formalImageVersionBusy) return;
  const affected = workspace.shots.filter(shot => targets.has(shot.shot_id) && shot.explicitly_changed);
  if (!affected.length) { showToast("所选镜头没有需要撤销的配图修改。"); return; }
  const active = () => state.formalImageWorkspace === workspace && formalRoute().projectId === projectId && current === creativeState();
  captureFormalImageInputs();
  current.imageUndoBusy = true;
  try {
    if (!await flushCreativeProgress({ manual: true }) || !active()) return;
    const saved = await api(formalImageWorkspacePath(projectId, workspace.project_version));
    if (!active()) return;
    if (saved.base_version_id !== workspace.base_version_id) { showToast("正式版本已变化，原选图仍保留，请重新读取后再撤销。"); return; }
    const ids = affected.map(shot => shot.shot_id);
    if (ids.some(id => !saved.shots?.some(shot => shot.shot_id === id))) { showToast("镜头已变化，原选图仍保留，请重新读取后再撤销。"); return; }
    const queue = creativeProgressQueue();
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/progress?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, { method: "PATCH", body: JSON.stringify({
      base_version_id: workspace.base_version_id, expected_project_version: workspace.project_version,
      expected_revision: queue.model.revision, expected_script_revision: Number(state.formalScript?.revision || 0),
      selections: ids.map(shot_id => ({shot_id, remove: true})), idempotency_key: formalRequestKey("undo-image-progress"),
    }) });
    if (!active()) return;
    queue.model = result;
    for (const id of ids) { queue.desired.delete(id); queue.baseline.delete(id); }
    invalidateFormalImageVersionProposal();
    for (const id of ids) {
      const index = workspace.shots.findIndex(shot => shot.shot_id === id);
      const original = saved.shots.find(shot => shot.shot_id === id);
      workspace.shots[index] = { ...original, explicitly_changed: false, library_candidate_key: "", library_candidate: null, query_input: original.query || "" };
      current.imageChanges.delete(id);
    }
    state.formalImageDirty = true;
    renderFormalImageWorkspace(workspace);
    captureFormalImageInputs();
    showToast(`已撤销 ${ids.length} 镜配图修改，恢复正式版本；其他镜头进度已保留。`);
  } catch (error) { if (active()) showToast(formalError(error)); }
  finally { current.imageUndoBusy = false; }
}

function toggleFormalImageTemporary(input) {
  const index = Number(input.dataset.imageShotTemporary);
  const shot = state.formalImageWorkspace?.shots?.[index];
  const card = elements.formalImageShotGrid.querySelector(`[data-image-shot="${index}"]`);
  if (!shot || !card) return;
  invalidateFormalImageVersionProposal();
  markCreativeImageChange(shot);
  shot.confirmed = false;
  shot.decision_kind = input.checked ? "temporary_text_basis" : "image_confirmed";
  card.querySelector(`[data-image-temporary-reason="${index}"]`).hidden = !input.checked;
  const select = card.querySelector(`[data-image-shot-select="${index}"]`);
  select.disabled = input.checked;
  if (!input.checked && !shot.selected_image_key) {
    const candidate = (shot.candidates || []).find((item) => item.image_key === select.value);
    if (formalImageCandidateHasRealPreview(candidate)) shot.selected_image_key = select.value;
  }
  card.querySelector(`[data-image-shot-confirm="${index}"]`).checked = false;
  refreshFormalImageReuseState();
  validateFormalImageSelections();
  if (!input.checked) loadFormalShotCandidatePreview(index);
}

function supplementFormalImageShot(index) {
  const fileInput = elements.formalImageSupplementFile;
  if (!fileInput) return;
  fileInput.dataset.imageSupplementShot = String(index);
  fileInput.click();
}

async function uploadFormalImageSupplement() {
  const projectId = formalRoute().projectId;
  const file = elements.formalImageSupplementFile.files?.[0];
  const index = Number(elements.formalImageSupplementFile.dataset.imageSupplementShot);
  const shot = state.formalImageWorkspace?.shots?.[index];
  if (!projectId || !file || !state.formalImageWorkspace || state.formalImageBusy) return;
  captureFormalImageInputs();
  const workspace = state.formalImageWorkspace;
  const button = elements.formalImageShotGrid.querySelector(`[data-image-supplement="${index}"]`);
  state.formalImageBusy = true;
  if (button) { button.disabled = true; button.textContent = "上传中…"; }
  const form = new FormData();
  form.append("file", file, file.name);
  try {
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/supplements?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(state.formalImageWorkspace.project_version)}`, {
      method: "POST", body: form,
    });
    if (state.formalImageWorkspace !== workspace || formalRoute().projectId !== projectId) return;
    creativeState().materialsKey = "";
    creativeState().imageKey = "";
    const params = new URLSearchParams({
      return_to: "shot-images",
      focus_shot: String(shot?.shot_no || ""),
      shot_id: String(shot?.shot_id || ""),
      supplement_image_key: String(result.image_key || ""),
    });
    navigateFormal(formalPath("materials", projectId, params));
    showToast("图片已加入，请分析后返回原镜头。");
  } catch (error) {
    showToast(formalError(error));
    elements.formalImageShotGrid.querySelector(`[data-image-shot="${index}"]`)?.focus({ preventScroll: true });
  } finally {
    state.formalImageBusy = false;
    if (button?.isConnected) { button.disabled = false; button.textContent = "上传图片"; }
    elements.formalImageSupplementFile.value = "";
  }
}

let creativeImageConfirmation = null;
function creativeImageConfirmationKey(projectId) {
  return `mplus-image-confirmation:${state.agentWorkspaceId}:${projectId}`;
}
async function previewCreativeImageConfirmation(scope) {
  captureFormalImageInputs();
  const projectId = formalRoute().projectId, workspace = state.formalImageWorkspace;
  if (!workspace) return;
  const ids = scope === "all" ? workspace.shots.map(shot => shot.shot_id) : [...creativeState().selectedShotIds];
  const chosen = workspace.shots.filter(shot => ids.includes(shot.shot_id));
  const panel = document.getElementById("creative-image-confirmation");
  panel.hidden = false;
  if (!chosen.length) { panel.textContent = "请先在镜头目录勾选本次要确认的镜头。"; return; }
  const retained = sessionStorage.getItem(creativeImageConfirmationKey(projectId));
  if (retained) {
    creativeImageConfirmation = JSON.parse(retained);
    panel.innerHTML = '<p>上次确认结果待核实，请恢复原请求。</p><button type="button" data-confirm-image-progress>核实并继续原确认</button>';
    return;
  }
  if (!await flushCreativeProgress({ manual: true })) return;
  if (formalRoute().projectId !== projectId || state.formalImageWorkspace !== workspace) return;
  const body = {base_version_id: workspace.base_version_id, expected_project_version: workspace.project_version,
    expected_revision: creativeProgressQueue()?.model?.revision || 0,
    expected_script_revision: state.formalScript.revision || 0,
    selections: chosen.map(shot => ({...creativeProgressSelection({...shot, explicitly_changed: true}),
      query: shot.query || "", checked: false})), idempotency_key: formalRequestKey("confirm-images")};
  panel.textContent = `正在核对 ${chosen.length} 镜的图片与复用说明…`;
  try {
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/confirmation-preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {method:"POST", body:JSON.stringify(body)});
    if (state.formalImageWorkspace !== workspace || formalRoute().projectId !== projectId) return;
    creativeImageConfirmation = {projectId, body: {...body, preview_digest:result.preview_digest,
      accepted_shot_ids:result.items.filter(item => item.processable).map(item => item.shot_id)}};
    panel.innerHTML = `<strong>本次 ${result.items.length} 镜 · 可确认 ${result.processable_count} 镜 · 待处理 ${result.pending_count} 镜</strong>${result.items.map(item => {
      const shot = workspace.shots.find(shot => shot.shot_id === item.shot_id);
      const key = shot.library_candidate_key ? `default:${shot.library_candidate_key}` : shot.selected_image_key;
      const url = state.formalImagePreviewUrls.get(key) || state.formalImageThumbnailUrls.get(key);
      return `<article><b>第 ${escapeHtml(item.shot_no)} 镜</b>${url ? `<img src="${escapeHtml(url)}" alt="第 ${escapeHtml(item.shot_no)} 镜待确认配图" style="max-width:180px;max-height:110px">` : '<span>请结合镜头中的图片核对</span>'}<p>${escapeHtml(item.reason || "可确认")}</p>${item.selection.reuse_reason ? `<p>复用说明：${escapeHtml(item.selection.reuse_reason)}</p>` : ""}</article>`;
    }).join("")}<button type="button" class="primary-button compact" data-confirm-image-progress ${result.processable_count ? "" : "disabled"}>确认可处理的 ${result.processable_count} 镜</button>`;
  } catch (error) { if (formalRoute().projectId === projectId) panel.textContent = formalError(error); }
}
async function confirmCreativeImageProgress() {
  const pending = creativeImageConfirmation, panel = document.getElementById("creative-image-confirmation");
  if (!pending || pending.busy || formalRoute().projectId !== pending.projectId) return;
  if (!sessionStorage.getItem(creativeImageConfirmationKey(pending.projectId))) {
    captureFormalImageInputs();
    if (!await flushCreativeProgress({manual:true}) || creativeImageConfirmation !== pending) return;
    if (creativeProgressQueue()?.model?.revision !== pending.body.expected_revision || state.formalScript?.revision !== pending.body.expected_script_revision) {
      panel.textContent = "预览后进度已变化，当前输入已保存，请重新预览确认范围。";
      return;
    }
  }
  const workspace = state.formalImageWorkspace, epoch = state.formalImageEpoch;
  const active = () => formalRoute().projectId === pending.projectId && formalRoute().page === "script"
    && state.formalImageEpoch === epoch && state.formalImageWorkspace === workspace;
  const confirmationQueue = creativeProgressQueue();
  const submittedInputs = new Map(pending.submittedInputs || (workspace?.shots || []).map(shot => [shot.shot_id, JSON.stringify(creativeProgressSelection(shot))]));
  pending.submittedInputs = [...submittedInputs];
  if (confirmationQueue) { confirmationQueue.confirming = true; window.clearTimeout(confirmationQueue.timer); }
  pending.busy = true;
  sessionStorage.setItem(creativeImageConfirmationKey(pending.projectId), JSON.stringify({projectId:pending.projectId, body:pending.body, submittedInputs: pending.submittedInputs}));
  panel.textContent = "正在确认并保存所选配图进度…";
  try {
    const result = await api(`/api/formal/projects/${encodeURIComponent(pending.projectId)}/image-workspace/confirm-progress?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {method:"POST",body:JSON.stringify(pending.body)});
    sessionStorage.removeItem(creativeImageConfirmationKey(pending.projectId));
    // Capture changes made while confirming, but keep the serial save queue paused.
    if (active()) captureFormalImageInputs();
    const queue = confirmationQueue;
    if (queue && Number(result.revision) >= Number(queue.model?.revision || 0)) {
      queue.model = result;
      for (const selection of result.selections || []) {
        if (!pending.body.accepted_shot_ids.includes(selection.shot_id)) continue;
        const shot = workspace?.shots.find(item => item.shot_id === selection.shot_id);
        if (!shot) continue;
        const latest = queue.desired.get(shot.shot_id) || creativeProgressSelection(shot);
        const changedDuringRequest = JSON.stringify(latest) !== submittedInputs.get(shot.shot_id);
        const confirmed = {...shot};
        applyCreativeProgressSelection(confirmed, selection);
        const baseline = creativeProgressSelection(confirmed);
        queue.baseline.set(shot.shot_id, JSON.stringify(baseline));
        queue.desired.set(shot.shot_id, changedDuringRequest ? latest : baseline);
        if (!changedDuringRequest) applyCreativeProgressSelection(shot, selection);
      }
    }
    sessionStorage.removeItem(formalImageInputKey(pending.projectId,pending.body.base_version_id));
    if (!active()) return;
    await loadFormalShotImages(pending.projectId, { capture: false });
    if (formalRoute().projectId !== pending.projectId || formalRoute().page !== "script") return;
    panel.hidden = false;
    panel.textContent = `已确认 ${result.confirmed_count} 镜并保存进度，剩余 ${result.pending_count} 镜保持待处理。核对全稿后可保存新版本。`;
    creativeImageConfirmation = null;
  } catch(error) {
    if (!active()) return;
    const code=error?.payload?.detail?.code;
    if (error.status >= 400 && error.status < 500 && error.status !== 408 && !["IMAGE_ADOPTION_PENDING","ACTION_RESULT_UNKNOWN"].includes(code)) {
      sessionStorage.removeItem(creativeImageConfirmationKey(pending.projectId));
      panel.textContent = `${formalError(error)} 原选择已保留，请重新预览。`;
    } else panel.innerHTML = '<p>确认结果待核实，原请求已保留。</p><button type="button" data-confirm-image-progress>核实并继续原确认</button>';
  } finally {
    pending.busy = false;
    if (confirmationQueue) {
      confirmationQueue.confirming = Boolean(sessionStorage.getItem(creativeImageConfirmationKey(pending.projectId)));
      if (!sessionStorage.getItem(creativeImageConfirmationKey(pending.projectId)) && creativeProgressPending(confirmationQueue))
        void flushCreativeProgress({queueOverride: confirmationQueue});
    }
  }
}

async function saveFormalImageVersion(event) {
  event.preventDefault();
  if (await openCreativeTool("save") === false) return;
  const confirm = elements.formalScriptSaveProposal.querySelector("[data-confirm-script-revision]");
  if (state.formalScriptRevisionProposal && !state.formalScriptRevisionProposal.response?.draft_only && confirm) {
    confirm.scrollIntoView({ block: "nearest" });
    confirm.focus({ preventScroll: true });
    return;
  }
  return saveFormalScriptVersion(event);
}

async function confirmFormalImageVersion() {
  captureFormalImageInputs();
  const projectId = formalRoute().projectId;
  const proposal = state.formalImageVersionProposal;
  if (!projectId || !proposal || state.formalImageVersionBusy) return;
  const workspace = state.formalImageWorkspace;
  const epoch = state.formalImageEpoch;
  const active = () => state.formalImageWorkspace === workspace && state.formalImageEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "script";
  const button = elements.formalImageVersionProposal.querySelector("[data-confirm-image-version]");
  state.formalImageVersionBusy = true;
  if (button) { button.disabled = true; button.textContent = "保存中…"; }
  elements.formalImageVersionState.textContent = "保存中…";
  elements.formalImageVersionMessage.hidden = false;
  elements.formalImageVersionMessage.textContent = "正在保存本次配图，请稍候…";
  validateFormalImageSelections();
  try {
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/proposals/${encodeURIComponent(proposal.proposal_id)}/confirm?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {
      method: "POST",
      body: JSON.stringify({ proposal_digest: proposal.proposal_digest }),
    });
    if (!active()) return;
    const completedUrl = new URL(window.location.href);
    completedUrl.searchParams.delete("revision_task_id");
    completedUrl.searchParams.delete("revision_shot");
    window.history.replaceState({}, "", `${completedUrl.pathname}${completedUrl.search}`);
    sessionStorage.removeItem(formalImageInputKey(projectId, state.formalImageWorkspace.base_version_id));
    state.formalImageDirty = false;
    state.formalImageRevisionTaskLoaded = "";
    const retainedInputs = new Map(workspace.shots.filter(shot => !proposal.submitted_shot_ids?.includes(shot.shot_id)).map(shot => [shot.shot_id, shot]));
    await Promise.all([loadFormalScript(projectId), refreshCreativeOverview(projectId)]);
    if (formalRoute().projectId !== projectId) return;
    for (const shot of result.shots || []) {
      const prior = retainedInputs.get(shot.shot_id);
      if (!prior) continue;
      for (const key of ["query_input", "temporary_reason", "reuse_reason"]) shot[key] = prior[key];
      if (prior.explicitly_changed) {
        for (const key of ["selected_image_key", "decision_kind", "confirmed", "query", "query_input"]) shot[key] = prior[key];
        markCreativeImageChange(shot);
      }
    }
    creativeState().imageKey = `${creativeState().versionId}:${state.formalScript?.project_version || ""}`;
    renderFormalImageWorkspace(result);
    captureFormalImageInputs();
    syncCreativeContext(state.formalScript);
    (result.shots || []).forEach((_, index) => loadFormalShotCandidatePreview(index));
    elements.formalImageShotGrid.querySelector("[data-image-shot]")?.focus({ preventScroll: true });
    elements.formalImageVersionMessage.hidden = false;
    elements.formalImageVersionMessage.innerHTML = result.review_eligible === false
      ? '配图已保存。补齐图片后即可审核。'
      : `配图已保存。<a class="primary-button compact" href="${formalPath("review", projectId)}" data-formal-target="1">进入审核</a>`;
    showToast("配图已保存为新版本。");
  } catch (error) {
    if (!active()) return;
    if (["PROJECT_VERSION_CONFLICT", "IMAGE_CANDIDATE_SNAPSHOT_DRIFT"].includes(error?.payload?.detail?.code)) await loadFormalShotImages(projectId);
    if (formalRoute().projectId !== projectId || formalRoute().page !== "script") return;
    elements.formalImageVersionState.textContent = "保存未完成";
    elements.formalImageVersionMessage.hidden = false;
    elements.formalImageVersionMessage.textContent = formalError(error);
    if (error?.payload?.detail?.code === "IMAGE_VERSION_AUDIT_FAILED") {
      // Audit rejection occurs before the version commit; its task is terminal.
      // Re-preview retained inputs instead of confirming the failed task again.
      invalidateFormalImageVersionProposal({ announce: false });
      elements.formalImageVersionMessage.insertAdjacentHTML("beforeend", '<button type="button" class="secondary-button compact" data-retry-image-version>重新预览版本影响</button>');
    }
    showToast(formalError(error));
  } finally {
    state.formalImageVersionBusy = false;
    if (button?.isConnected) { button.disabled = false; button.textContent = "确认并创建新版本"; }
    if (formalRoute().projectId === projectId && formalRoute().page === "script") validateFormalImageSelections();
  }
}

function formalImageValues(value) {
  return String(value || "").split(/[,\n，、]+/).map((item) => item.trim()).filter(Boolean);
}

async function submitFormalImageAnalysis(event) {
  event.preventDefault();
  const form = event.target.closest("[data-formal-image-form]");
  const projectId = formalRoute().projectId;
  const routeParams = new URLSearchParams(window.location.search);
  const returnTo = routeParams.get("return_to") || "";
  const returnShot = routeParams.get("focus_shot") || "";
  const supplementImageKey = routeParams.get("supplement_image_key") || "";
  const index = Number(form?.dataset.formalImageForm);
  const image = state.formalImages?.images?.[index];
  if (!form || !projectId || !image || state.formalImageBusy || !form.reportValidity()) return;
  const button = form.querySelector('button[type="submit"]');
  let epoch = state.formalImageEpoch, imageModel = state.formalImages;
  const active = () => state.formalImageEpoch === epoch && state.formalImages === imageModel && formalRoute().projectId === projectId && formalSurface() === "materials";
  captureFormalAnalysisForms();
  const submittedValues = formalAnalysisFormValues(form);
  const draftKey = form.dataset.analysisDraftKey;
  const data = new FormData(form);
  state.formalImageBusy = true;
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    if (!active()) return;
    const readiness = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/images/${encodeURIComponent(image.image_key)}/analysis?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {
      method: "POST",
      body: JSON.stringify({
        expected_project_version: project.project_version,
        expected_analysis_key: image.analysis_key || "",
        status: data.get("status"),
        caption: String(data.get("caption") || "").trim(),
        tags: formalImageValues(data.get("tags")),
        subjects: formalImageValues(data.get("subjects")),
        scene: String(data.get("scene") || "").trim(),
        purpose: String(data.get("purpose") || "").trim(),
        quality: String(data.get("quality") || "").trim(),
        brand: String(data.get("brand") || "").trim(),
        evidence: formalImageValues(data.get("evidence")),
        quality_flags: data.getAll("quality_flags"),
        negative_flags: data.getAll("negative_flags"),
      }),
    });
    if (!active()) return;
    captureFormalAnalysisForms();
    if (JSON.stringify(formalAnalysisFormValues(form)) === JSON.stringify(submittedValues)) {
      formalAnalysisDrafts.delete(draftKey);
      form.dataset.analysisBaseline = JSON.stringify(submittedValues);
    }
    releaseFormalImagePreviews();
    epoch = state.formalImageEpoch;
    renderFormalImages(readiness, project.project_version);
    imageModel = state.formalImages;
    state.formalImageBusy = false;
    elements.formalImageGrid.querySelectorAll(".ui00-image-card")[index]?.focus({ preventScroll: true });
    await loadFormalImagePreviews(projectId, project.project_version, state.formalImageEpoch);
    if (!active()) return;
    if (returnTo === "shot-images" && returnShot && image.image_key === supplementImageKey) {
      navigateFormal(formalPath("shot-images", projectId, materialsReturnTarget(routeParams)));
      return;
    }
    showToast("图片理解已保存。");
  } catch (error) {
    if (!active()) return;
    if (["PROJECT_VERSION_CONFLICT", "IMAGE_ANALYSIS_VERSION_CONFLICT"].includes(error?.payload?.detail?.code)) await loadFormalImages(projectId);
    showToast(formalError(error));
  } finally {
    if (active()) state.formalImageBusy = false;
    if (active() && button.isConnected) { button.disabled = false; button.textContent = "保存人工理解"; }
  }
}

async function understandFormalImages() {
  const projectId = formalRoute().projectId;
  if (!projectId || state.formalImageAutoBusy || !state.formalImages?.pending) return;
  let epoch = state.formalImageEpoch, imageModel = state.formalImages;
  const active = () => state.formalImageEpoch === epoch && state.formalImages === imageModel && formalRoute().projectId === projectId && formalSurface() === "materials";
  state.formalImageAutoBusy = true;
  elements.formalImageUnderstand.disabled = true;
  elements.formalImageUnderstand.textContent = "正在分析…";
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    if (!active()) return;
    const readiness = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/images/understand?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {
      method: "POST",
      body: JSON.stringify({ expected_project_version: project.project_version }),
    });
    if (!active()) return;
    releaseFormalImagePreviews();
    epoch = state.formalImageEpoch;
    renderFormalImages(readiness, project.project_version);
    imageModel = state.formalImages;
    await loadFormalImagePreviews(projectId, project.project_version, state.formalImageEpoch);
    if (!active()) return;
    const result = readiness.automatic_understanding || {};
    const message = Number(result.needs_review || 0) > 0
      ? `已分析 ${Number(result.processed || 0)} 张，${Number(result.needs_review || 0)} 张需要人工核对。`
      : `已分析 ${Number(result.processed || 0)} 张图片。`;
    showToast(message);
    const params = new URLSearchParams(window.location.search);
    const supplemented = (readiness.images || []).find((image) => image.image_key === params.get("supplement_image_key"));
    if (params.get("return_to") === "shot-images" && params.get("focus_shot") && supplemented?.status === "pass") {
      navigateFormal(formalPath("shot-images", projectId, materialsReturnTarget(params)));
      return;
    }
    elements.formalImageGrid.querySelector(".ui00-image-card[data-state='low_confidence'], .ui00-image-card[data-state='blocked'], .ui00-image-card[data-state='failed'], .ui00-image-card")?.focus({ preventScroll: true });
  } catch (error) {
    if (!active()) return;
    if (error?.payload?.detail?.code === "PROJECT_VERSION_CONFLICT") {
      await loadFormalImages(projectId);
    }
    showToast(formalError(error));
  } finally {
    if (active()) {
      state.formalImageAutoBusy = false;
      elements.formalImageUnderstand.textContent = "分析图片";
      elements.formalImageUnderstand.disabled = Number(state.formalImages?.pending || 0) < 1;
    }
  }
}

async function uploadFormalProjectImages(event) {
  event.preventDefault();
  const projectId = formalRoute().projectId;
  const files = [...elements.formalImageUploadFiles.files];
  if (!projectId || !files.length || state.formalImageBusy) { if (!files.length) showToast("请先选择图片。"); return; }
  let epoch = state.formalImageEpoch;
  const active = () => state.formalImageEpoch === epoch && formalRoute().projectId === projectId && formalSurface() === "materials";
  state.formalImageBusy = true;
  elements.formalImageUploadButton.disabled = true;
  elements.formalImageUploadButton.textContent = "上传中…";
  let uploaded = 0;
  let added = 0, repeated = 0;
  const knownImageKeys = new Set((state.formalImages?.images || []).map(image => image.image_key));
  let sending = false;
  const fileResults = files.map(file => ({ name: file.name, status: "未上传" }));
  const resultsHtml = () => `<ul>${fileResults.map(item => `<li>${escapeHtml(item.name)}：${escapeHtml(item.status)}</li>`).join("")}</ul>`;
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    if (!active()) return;
    let projectVersion = project.project_version;
    for (const file of files) {
      const data = new FormData();
      data.append("file", file, file.name);
      fileResults[uploaded].status = "上传中";
      elements.formalImageUploadState.innerHTML = resultsHtml();
      sending = true;
      const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/supplements?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(projectVersion)}`, { method: "POST", body: data });
      if (!active()) return;
      sending = false;
      const alreadyPresent = knownImageKeys.has(result.image_key);
      if (alreadyPresent) repeated += 1;
      else { added += 1; knownImageKeys.add(result.image_key); }
      fileResults[uploaded].status = alreadyPresent ? "已有相同图片，已复用" : "已新增";
      uploaded += 1;
      projectVersion = result.project_version ?? (await api(`/api/formal/projects/${encodeURIComponent(projectId)}`)).project_version;
      if (!active()) return;
      elements.formalImageUploadState.innerHTML = `<p>已上传 ${uploaded} / ${files.length} 张</p>${resultsHtml()}`;
    }
    elements.formalImageUploadFiles.value = "";
    elements.formalImageUploadSelection.textContent = "尚未选择图片";
    const loading = loadFormalImages(projectId);
    epoch = state.formalImageEpoch;
    state.formalImageBusy = true;
    await loading;
    if (!active()) return;
    const receipt = `新增 ${added} 张图片${repeated ? `，复用已有 ${repeated} 张` : ""}。`;
    elements.formalImageUploadState.innerHTML = `<strong>${receipt}</strong>${resultsHtml()}<p>待分析的图片需完成“分析图片”后才能选用。</p>`;
    elements.formalImageUnderstand.focus({ preventScroll: true });
    showToast(receipt);
  } catch (error) {
    if (!active()) return;
    const detail = error?.payload?.detail || {};
    if (sending) fileResults[uploaded].status = "未完成，请核对后重试";
    // Keep only unresolved inputs. Successfully accepted files remain in the
    // server pool and must not be included in the next explicit submission.
    const remaining = new DataTransfer();
    files.slice(uploaded).forEach(file => remaining.items.add(file));
    elements.formalImageUploadFiles.files = remaining.files;
    elements.formalImageUploadSelection.textContent = remaining.files.length
      ? `待处理 ${remaining.files.length} 张；可重新选择修正后的文件` : "尚未选择图片";
    if (uploaded) {
      const loading = loadFormalImages(projectId);
      epoch = state.formalImageEpoch;
      state.formalImageBusy = true;
      await loading;
      if (!active()) return;
    }
    elements.formalImageUploadState.innerHTML = `<strong>${uploaded ? `已处理 ${uploaded} 张（新增 ${added}，复用 ${repeated}）；请处理剩余文件` : "图片上传未完成"}</strong>${resultsHtml()}<p>${escapeHtml(formalError(error))}</p>${detail.next_href ? `<a class="secondary-button compact" href="${escapeHtml(detail.next_href)}">${escapeHtml(detail.next_label || "确认资料")}</a>` : ""}`;
  } finally {
    if (active()) {
      state.formalImageBusy = false;
      elements.formalImageUploadButton.disabled = false;
      elements.formalImageUploadButton.textContent = "上传图片";
    }
  }
}

function formalReviewPath(projectId, action = "", projectVersion = null) {
  const suffix = action ? `/${encodeURIComponent(action)}` : "";
  const query = new URLSearchParams({ workspace_id: state.agentWorkspaceId });
  if (projectVersion !== null && projectVersion !== undefined) {
    query.set("expected_project_version", String(projectVersion));
  }
  const viewedVersion = new URLSearchParams(window.location.search).get("version_id");
  if (!action && viewedVersion) query.set("version_id", viewedVersion);
  const viewedNumber = new URLSearchParams(window.location.search).get("version_no");
  if (!action && !viewedVersion && viewedNumber) query.set("version_no", viewedNumber);
  return `/api/formal/projects/${encodeURIComponent(projectId)}/review${suffix}?${query}`;
}

function formalReviewStateLabel(value) {
  return ({
    empty: "等待版本",
    blocked: "前置未完成",
    ready: "需审核",
    reviewing: "审核中",
    needs_change: "需修改",
    ready_final: "待整片确认",
    confirmed: "整片已确认",
  })[value] || "正在读取";
}

function formalReviewSourceLabel(source) {
  return formalImageSourceLabel(source);
}

function formalReviewImageAlt(issue, index) {
  const label = issue?.read_only || issue?.preview || issue?.preview_only || !issue?.issue_key ? "配图预览" : "已确认图片";
  return `镜头 ${issue?.shot_no || index + 1} ${label}：${issue?.image_caption || "当前项目图片"}`;
}

function formalReviewImage(issue, index) {
  const url = issue?.image_key ? state.formalImagePreviewUrls.get(issue.image_key) : "";
  return `<div class="ui00-review-image" id="formal-review-image-${index}">${url ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(formalReviewImageAlt(issue, index))}" width="1280" height="720" loading="lazy" decoding="async" />` : `<span>${issue?.image_key ? "正在读取图片预览…" : "本镜尚未配图"}</span>`}</div>`;
}

function formalReviewIssue(issue, index, reviewStarted) {
  const readOnly = Boolean(state.formalReview?.read_only || issue.read_only || issue.preview_only || !issue.issue_key);
  const open = !readOnly && ["open", "recheck"].includes(issue.status);
  const returned = issue.status === "return";
  const inherited = issue.status === "approve" && issue.inherited_review;
  const inheritedNote = inherited ? `<p class="ui00-review-inherited" data-review-inherited>沿用${escapeHtml(inherited.version_label)} 的图片审核 · ${escapeHtml(inherited.reviewer)} · <time datetime="${escapeHtml(inherited.reviewed_at)}">${escapeHtml(formalScriptDate(inherited.reviewed_at))}</time></p>` : "";
  const selection = reviewStarted && open ? `<label class="ui00-review-select"><input type="checkbox" data-review-select="${index}" />选择本项</label>` : "";
  const controls = reviewStarted && open ? `<div class="ui00-review-item-actions"><div class="ui00-review-actions-row"><button class="secondary-button compact" type="button" data-review-decision="return" data-review-index="${index}">退回修改</button><button class="secondary-button compact" type="button" data-review-decision="approve" data-review-index="${index}">通过本项</button></div><details data-review-note-panel="${index}"><summary>补充意见</summary><label for="formal-review-note-${index}"><span>意见 · 退回时必填</span><textarea id="formal-review-note-${index}" name="review_note_${index}" data-review-note="${index}" rows="2" maxlength="4000" autocomplete="off" placeholder="说明需要修改的内容"></textarea></label></details></div>` : "";
  const repair = issue.repair_action;
  const repairHref = repair?.href ? formalCanonicalPath(repair.href) : repair?.shot_id ? formalPath("script", formalRoute().projectId, { panel: repair.panel || "script", shot_id: repair.shot_id }) : "";
  const next = readOnly ? `<p class="ui00-review-return-note">当前为问题预览，尚未开始人工审核。</p>${repairHref ? `<a class="secondary-button compact" data-review-repair href="${escapeHtml(repairHref)}">${escapeHtml(repair.label || "返回镜头处理")}</a>` : ""}` : returned ? '<p class="ui00-review-return-note">已退回，修改并保存新版本后再审核。</p>' : "";
  return `<article class="ui00-review-item" data-review-item-index="${index}" data-severity="${escapeHtml(issue.severity)}" data-status="${escapeHtml(issue.status)}" tabindex="-1"><header><div>${selection}<span>镜头 ${escapeHtml(issue.shot_no || "整片")}</span><h3>${escapeHtml(issue.title || "检查当前镜头")}</h3></div><div class="ui00-review-item-badges"><b>${escapeHtml(issue.severity_label || "提醒")}</b><em>${escapeHtml(issue.status_label || "待处理")}</em></div></header>${inheritedNote}<div class="ui00-review-item-grid"><div><p class="ui00-review-visual">${escapeHtml(issue.visual || "当前镜头")}</p><details class="ui00-review-item-source"><summary>内容与来源</summary><dl><div><dt>检查范围</dt><dd>${escapeHtml(issue.change_summary || "本镜内容与图片")}</dd></div><div><dt>图片说明</dt><dd>${escapeHtml(issue.image_caption || "当前已确认图片")}</dd></div><div><dt>资料来源</dt><dd>${escapeHtml(formalReviewSourceLabel(issue.source))}</dd></div></dl></details></div>${formalReviewImage(issue, index)}</div>${controls}${next}</article>`;
}

function selectFormalReviewGroup(group = state.formalReviewSelectedGroup) {
  if (!state.formalReview?.review_started || state.formalReviewBusy) return;
  state.formalReviewSelectedGroup = group;
  (state.formalReview.issues || []).forEach((issue, index) => {
    if ((issue.focus_required === false ? "others" : "focused") !== group || !["open", "recheck"].includes(issue.status)) return;
    const input = elements.formalReviewIssues.querySelector(`[data-review-select="${index}"]`);
    if (input) input.checked = true;
  });
  validateFormalReviewControls();
}

function renderFormalReviewEvidence(model) {
  const evidence = Array.isArray(model?.evidence) ? model.evidence : [];
  if (!evidence.length) {
    elements.formalReviewEvidence.innerHTML = '<div class="ui00-business-empty"><strong>尚无人工审核记录</strong><p>开始审核后，系统会在这里显示可核验的业务记录。</p></div>';
    return;
  }
  elements.formalReviewEvidence.innerHTML = `<ol>${evidence.map((item) => `<li><strong>${escapeHtml(item.summary || "审核记录")}</strong><span>${escapeHtml(formalScriptDate(item.created_at))}</span><em>${item.evidence_status === "available" ? "证据可用" : "证据需复核"}</em></li>`).join("")}</ol>`;
}

function validateFormalReviewControls() {
  const model = state.formalReview;
  elements.formalReviewFinalSubmit.disabled = Boolean(
    state.formalReviewBusy || model?.read_only || !model?.ready_for_final
  );
  const selected = elements.formalReviewIssues.querySelectorAll("[data-review-select]:checked").length;
  elements.formalReviewBatch.hidden = model?.read_only || !model?.review_started || !model?.issues?.some((issue) => ["open", "recheck"].includes(issue.status));
  elements.formalReviewBatchCount.textContent = `已选 ${selected} 项`;
  elements.formalReviewBatchApprove.disabled = state.formalReviewBusy || selected === 0;
  elements.creativeReviewBatchReturn.disabled = state.formalReviewBusy || selected === 0 || !elements.creativeReviewReturnNote.value.trim();
}

function captureFormalReviewNotes() {
  const values = new Map();
  (state.formalReview?.issues || []).forEach((issue, index) => {
    const input = elements.formalReviewIssues.querySelector(`[data-review-note="${index}"]`);
    if (issue.issue_key && input) values.set(issue.issue_key, input.value);
  });
  const selected = new Set(Array.from(elements.formalReviewIssues.querySelectorAll("[data-review-select]:checked")).map((input) => state.formalReview?.issues?.[Number(input.dataset.reviewSelect)]?.issue_key).filter(Boolean));
  return { projectId: state.formalReviewProjectId, baseVersionId: state.formalReview?.base_version_id, values, selected, commonNote: elements.creativeReviewReturnNote?.value || "" };
}

function renderFormalReview(model, focusIndex = null, notes = captureFormalReviewNotes()) {
  state.formalReview = model;
  state.formalReviewProjectId = formalRoute().projectId || "";
  elements.creativeReviewReturnNote.value = notes.projectId === state.formalReviewProjectId && notes.baseVersionId === model?.base_version_id ? notes.commonNote || "" : "";
  const overview = state.formalReviewOverview;
  const savedLabel = overview?.saved_delivery?.version_label || "";
  const hasDraft = Boolean(overview?.draft?.can_save_version);
  elements.creativeReviewDraft.hidden = !hasDraft && overview?.draft?.state !== "unknown";
  elements.creativeReviewDraft.innerHTML = hasDraft ? `<strong>当前稿尚未保存为版本</strong><p>这里显示${escapeHtml(model?.version_label || "已保存版本")}的审核状态${savedLabel ? `；${escapeHtml(savedLabel)}为此前已确认版本` : ""}。</p><a class="primary-button compact" data-formal-target href="${formalPath("script", formalRoute().projectId, { panel: "script", action: "save" })}">保存当前稿</a>` : '<strong>当前稿状态暂时无法核实，请重新读取后继续。</strong>';
  if (model?.read_only) {
    elements.creativeReviewDraft.hidden = false;
    elements.creativeReviewDraft.innerHTML = `<strong>正在查看${escapeHtml(model.version_label)} · 已保存内容，只读</strong><p>审核记录与交付文件均属于本版。</p><a class="secondary-button compact" data-formal-target href="${formalPath("review", formalRoute().projectId)}">返回当前版本审核</a>`;
  }
  const active = Boolean(model && !model.read_only && !["empty", "blocked"].includes(model.state));
  const decision = model?.decision || {};
  elements.formalReviewState.textContent = formalReviewStateLabel(model?.state);
  elements.formalReviewDecisionTitle.textContent = decision.label || (model?.state === "blocked" ? "当前版本暂不能审核" : "等待可审核版本");
  elements.formalReviewDecisionCopy.textContent = decision.copy || model?.message || "完成脚本与逐镜图片确认后再进入审核。";
  const editActions = Array.isArray(model?.edit_actions) ? model.edit_actions : [];
  if (model?.state === "needs_change" && editActions.length) {
    elements.formalReviewRevisionProposal.hidden = false;
    elements.formalReviewRevisionProposal.innerHTML = `<strong>退回已记录</strong><p>选择需要修改的内容。审核页不会自动创建子版本。</p><div class="ui00-inline-actions">${editActions.map((action) => `<a class="secondary-button compact" href="${escapeHtml(action.href)}">${escapeHtml(action.label)}</a>`).join("")}</div>`;
  } else {
    elements.formalReviewRevisionProposal.hidden = true;
    elements.formalReviewRevisionProposal.replaceChildren();
  }
  const countLabel = value => Number.isInteger(value) && value >= 0 ? String(value) : "—";
  elements.formalReviewProblemCount.textContent = countLabel(decision.problem_count);
  elements.formalReviewSeriousCount.textContent = countLabel(decision.serious_count);
  elements.formalReviewAttentionCount.textContent = countLabel(decision.attention_count);
  elements.formalReviewApprovedCount.textContent = countLabel(decision.approved_count);
  elements.formalReviewStart.hidden = !active || Boolean(model.review_started) || model.state === "confirmed";
  elements.formalReviewStart.disabled = state.formalReviewBusy;
  const expandedReview = new Set([...elements.formalReviewIssues.querySelectorAll("details[open]")].map(item => item.querySelector("summary")?.textContent));
  const issues = Array.isArray(model?.issues) ? model.issues : [];
  const focused = [], others = [];
  issues.forEach((issue, index) => (issue.focus_required === false || model.state === "blocked" && issue.is_prerequisite === false ? others : focused).push({ issue, index }));
  const cards = (items) => items.map(({ issue, index }) => formalReviewIssue(issue, index, Boolean(model.review_started))).join("");
  if (!focused.length) state.formalReviewSelectedGroup = "others";
  const groupButton = (name, items) => model.review_started && items.some(({ issue }) => ["open", "recheck"].includes(issue.status)) ? `<button class="secondary-button compact" type="button" data-review-select-group="${name}">选择本组待审核项</button>` : "";
  const clearCopy = model.state === "confirmed" ? "本版审核已完成，可展开查看审核记录。"
    : model.review_started
    ? "当前没有问题或变化镜头。展开其他镜头核对后，可选择批量通过。"
    : model.state === "blocked" ? "以下为只读预览，请先完成前置步骤，再开始人工审核。" : "展开其他镜头预览，开始人工审核后可登记决定。";
  elements.formalReviewIssues.innerHTML = issues.length
    ? `${focused.length ? `<section data-review-group="focused"><div class="ui00-review-group-title"><h3>问题与变化 · ${focused.length} 项</h3>${groupButton("focused", focused)}</div>${cards(focused)}</section>` : `<p class="ui00-review-clear">${clearCopy}</p>`}${others.length ? `<details class="ui00-review-others" data-review-group="others"><summary>其他镜头 · ${others.length} 项</summary><div class="ui00-review-group-title">${groupButton("others", others)}</div>${cards(others)}</details>` : ""}`
    : `<div class="ui00-business-empty"><strong>${model?.state === "confirmed" ? "当前版本已完成整片确认" : "当前没有可审核镜头"}</strong><p>${escapeHtml(model?.message || "请先完成前置步骤。")}</p></div>`;
  elements.formalReviewIssues.querySelectorAll("details").forEach(item => { if (expandedReview.has(item.querySelector("summary")?.textContent)) item.open = true; });
  if (notes.projectId === state.formalReviewProjectId) {
    // Server-issued issue keys identify this version's review items, even when the project lock changes.
    issues.forEach((issue, index) => {
      const input = elements.formalReviewIssues.querySelector(`[data-review-note="${index}"]`);
      if (input && ["open", "recheck"].includes(issue.status)) input.value = notes.values.get(issue.issue_key) || "";
      const selected = elements.formalReviewIssues.querySelector(`[data-review-select="${index}"]`);
      if (selected && ["open", "recheck"].includes(issue.status)) selected.checked = Boolean(notes.selected?.has(issue.issue_key));
    });
  }
  elements.formalReviewTraceability.innerHTML = planTraceTable(
    Array.isArray(model?.traceability) ? model.traceability : []
  );
  elements.formalReviewDecisionCounts.innerHTML = planDecisionCountsMarkup(
    model?.decision_counts || {}, "review"
  );
  renderFormalReviewEvidence(model);
  document.querySelector("#formal-review-final-title").textContent = model?.read_only
    ? `${model.version_label} · 整片确认记录` : "确认当前完整版本";
  elements.formalReviewFinalCopy.textContent = model?.state === "confirmed"
    ? `已由有权限的项目经理完成确认${model.final_confirmation?.confirmed_at ? ` · ${formalScriptDate(model.final_confirmation.confirmed_at)}` : ""}。`
    : model?.ready_for_final
    ? "全部问题镜头和变化镜头已处理。请核对整片后完成确认。"
    : "处理全部待审核项后，请核对整片并确认。";
  elements.formalReviewFinalForm.hidden = model?.read_only || model?.state === "confirmed";
  elements.formalReviewNextAction.textContent = model?.next_action || model?.message || "等待可执行的下一步。";
  const deliveryReady = model?.state === "confirmed";
  elements.formalDeliveryCard.hidden = false;
  document.querySelector("#delivery-current-version").textContent = model?.version_label || "尚无可交付版本";
  const prerequisites = model?.prerequisites || (model?.state === "blocked" ? (model.issues || []).filter(issue => issue.repair_action).map(issue => ({label: issue.repair_action.label, href: issue.repair_action.href, reason: issue.change_summary || issue.title})) : []);
  const prerequisitePanel = document.querySelector("#review-prerequisites");
  prerequisitePanel.hidden = !prerequisites.length;
  const groups = model?.prerequisite_groups;
  prerequisitePanel.innerHTML = groups?.length ? `<h3>先完成这些事项</h3>${groups.map(group => `<section class="review-prerequisite-group"><div><strong>${escapeHtml(group.label)}</strong><a class="secondary-button compact" data-formal-target href="${escapeHtml(group.href)}">${escapeHtml(group.action_label)}</a></div><details><summary>查看具体镜头与原因</summary><ul>${(group.items || []).map(item => `<li><span>${item.shot_no ? `第 ${escapeHtml(item.shot_no)} 镜 · ` : ""}${escapeHtml(item.reason)}</span><a data-formal-target href="${escapeHtml(item.href)}">处理</a></li>`).join("")}</ul></details></section>`).join("")}` : prerequisites.length ? `<h3>先完成这些事项</h3><ul>${prerequisites.map(item => `<li><span>${escapeHtml(item.reason || item.label)}</span><a class="secondary-button compact" data-formal-target href="${escapeHtml(item.href)}">${escapeHtml(item.label)}</a></li>`).join("")}</ul>` : "";
  elements.formalDeliveryExcel.disabled = !deliveryReady || state.formalDeliveryBusy;
  elements.formalDeliveryPdf.disabled = !deliveryReady || state.formalDeliveryBusy;
  elements.formalDeliveryStatus.textContent = deliveryReady
    ? `${model.version_label || "已确认版本"}可导出；这不会发布项目或切换生产版本。`
    : `${model?.version_label || "当前项目"}尚未满足正式交付条件：${model?.message || model?.next_action || "需完成逐镜配图、人工审核及整片确认"}。处理入口见上方前置事项及审核操作。`;
  validateFormalReviewControls();
  if (focusIndex !== null) {
    elements.formalReviewIssues.querySelector(`[data-review-item-index="${focusIndex}"]`)?.focus({ preventScroll: true });
  }
}

async function loadFormalReviewPreviews(projectId, model, epoch) {
  const issues = Array.isArray(model?.issues) ? model.issues : [];
  const byKey = new Map();
  issues.forEach((issue, index) => {
    if (issue.image_key) byKey.set(issue.image_key, [...(byKey.get(issue.image_key) || []), index]);
  });
  await Promise.all([...byKey].map(async ([imageKey, indexes]) => {
    try {
      const path = `/api/formal/projects/${encodeURIComponent(projectId)}/images/${encodeURIComponent(imageKey)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(model.project_version)}`;
      const url = await requestFormalPreview(path, { candidate: { image_key: imageKey }, model,
        owner: `review:${projectId}:${epoch}`, priority: 1 });
      if (state.formalReviewEpoch !== epoch || formalRoute().projectId !== projectId || formalRoute().page !== "review") return;
      state.formalImagePreviewUrls.set(imageKey, url);
      indexes.forEach((index) => {
        const target = document.querySelector(`#formal-review-image-${index}`);
        const issue = issues[index];
        if (!target?.isConnected) return;
        const image = document.createElement("img");
        image.src = url;
        image.alt = formalReviewImageAlt(issue, index);
        image.width = 1280;
        image.height = 720;
        image.loading = "lazy";
        image.decoding = "async";
        target.replaceChildren(image);
      });
    } catch (error) {
      indexes.forEach((index) => {
        const target = document.querySelector(`#formal-review-image-${index}`);
        if (state.formalReviewEpoch === epoch && target?.isConnected) target.innerHTML = `<span>${escapeHtml(formalError(error))}</span>`;
      });
    }
  }));
}

async function loadFormalReview(projectId) {
  const notes = captureFormalReviewNotes();
  releaseFormalImagePreviews();
  state.formalReviewSelectedGroup = "focused";
  state.formalReviewEpoch += 1;
  const requestEpoch = state.formalReviewEpoch;
  state.formalReview = null;
  state.formalReviewBusy = false;
  elements.formalReviewStart.textContent = "开始审核";
  elements.formalReviewFinalSubmit.textContent = "完成整片确认";
  validateFormalReviewControls();
  state.formalDeliveryBusy = false;
  elements.formalReviewStart.hidden = true;
  elements.formalDeliveryCard.hidden = true;
  elements.formalDeliveryExcel.disabled = true;
  elements.formalDeliveryPdf.disabled = true;
  elements.formalDeliveryStatus.textContent = "正在核对当前确认版本…";
  elements.formalReviewIssues.innerHTML = '<div class="ui00-business-empty"><strong>正在读取当前版本</strong><p>请稍候，审核操作会在状态核对完成后开放。</p></div>';
  elements.formalReviewTraceability.innerHTML = '<div class="workbench-empty">正在读取需求追踪记录…</div>';
  elements.formalReviewDecisionCounts.innerHTML = planDecisionCountsMarkup({});
  const available = Boolean(projectId && state.formalBriefAvailable && state.agentAuth && state.agentWorkspaceId);
  elements.formalReviewProjectRequired.hidden = available;
  elements.formalReviewWorkbench.hidden = !available;
  if (!available) { elements.formalReviewState.textContent = "等待项目"; return; }
  elements.formalReviewState.textContent = "读取中";
  for (const counter of [elements.formalReviewProblemCount, elements.formalReviewSeriousCount, elements.formalReviewAttentionCount, elements.formalReviewApprovedCount]) counter.textContent = "—";
  elements.formalReviewDecisionTitle.textContent = "正在读取当前版本";
  elements.formalReviewDecisionCopy.textContent = "正在核对逐镜图片、机器检查和人工审核记录。";
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    const [model, overview] = await Promise.all([api(formalReviewPath(projectId, "", project.project_version)), api(`/api/formal/projects/${encodeURIComponent(projectId)}/overview`).catch(() => ({ draft: { state: "unknown" } }))]);
    if (
      state.formalReviewEpoch !== requestEpoch
      || formalRoute().projectId !== projectId
      || formalRoute().page !== "review"
    ) return;
    state.formalReviewOverview = overview;
    renderFormalReview(model, null, notes);
    await loadFormalReviewPreviews(projectId, model, requestEpoch);
  } catch (error) {
    if (
      state.formalReviewEpoch !== requestEpoch
      || formalRoute().projectId !== projectId
      || formalRoute().page !== "review"
    ) return;
    renderFormalReview({
      state: "blocked",
      message: formalError(error),
      issues: [],
      production_certification: "BLOCK",
    });
  }
}

async function startFormalReview() {
  if (state.formalReview?.read_only) return;
  const projectId = formalRoute().projectId;
  if (!projectId || state.formalReviewBusy || !state.formalReview?.project_version) return;
  const review = state.formalReview;
  const epoch = state.formalReviewEpoch;
  const active = () => state.formalReviewEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "review";
  state.formalReviewBusy = true;
  elements.formalReviewStart.disabled = true;
  elements.formalReviewStart.textContent = "正在登记…";
  try {
    const model = await api(formalReviewPath(projectId, "start"), {
      method: "POST",
      body: JSON.stringify({
        expected_project_version: state.formalReview.project_version,
        idempotency_key: formalRequestKey("review-start"),
      }),
    });
    if (!active() || state.formalReview !== review) return;
    renderFormalReview(model, 0);
    showToast("当前版本审核项已登记。请逐项核对后给出人工结论。");
  } catch (error) {
    if (!active()) return;
    if (error?.payload?.detail?.code === "PROJECT_VERSION_CONFLICT") await loadFormalReview(projectId);
    else showToast(formalError(error));
  } finally {
    if (state.formalReviewEpoch === epoch) state.formalReviewBusy = false;
    if (!active()) return;
    elements.formalReviewStart.textContent = "开始审核";
    validateFormalReviewControls();
  }
}

async function submitFormalReviewDecision(index, decision, note) {
  return submitFormalReviewDecisions([index], decision, note);
}

async function submitFormalReviewBatch() {
  const indexes = Array.from(elements.formalReviewIssues.querySelectorAll("[data-review-select]:checked")).map((input) => Number(input.dataset.reviewSelect));
  return submitFormalReviewDecisions(indexes, "approve", "");
}

async function submitFormalReviewDecisions(indexes, decision, note = "") {
  if (state.formalReview?.read_only) return;
  const projectId = formalRoute().projectId;
  const review = state.formalReview;
  const issues = [...new Set(indexes)].map((index) => review?.issues?.[index]);
  if (!projectId || state.formalReviewProjectId !== projectId || !issues.length || issues.some((issue) => !issue?.issue_key || issue?.read_only || issue?.preview_only || !["open", "recheck"].includes(issue.status)) || state.formalReviewBusy || (decision === "return" && !note.trim())) return;
  const epoch = state.formalReviewEpoch;
  const active = () => state.formalReviewEpoch === epoch && state.formalReview === review && formalRoute().projectId === projectId && formalRoute().page === "review";
  state.formalReviewBusy = true;
  validateFormalReviewControls();
  try {
    const model = await api(formalReviewPath(projectId, "decisions"), {
      method: "POST",
      body: JSON.stringify({
        expected_project_version: state.formalReview.project_version,
        issue_keys: issues.map((issue) => issue.issue_key),
        decision,
        ...(note.trim() ? { note: note.trim() } : {}),
        idempotency_key: formalRequestKey(`review-${decision}`),
      }),
    });
    if (!active()) return;
    renderFormalReview(model, indexes.length === 1 ? indexes[0] : null);
    if (decision === "return" && elements.creativeReviewReturnNote.value.trim() === note.trim()) elements.creativeReviewReturnNote.value = "";
    showToast(decision === "approve" ? `${issues.length} 项已通过。` : "已退回，修改后再审核。");
  } catch (error) {
    if (!active()) return;
    if (error?.payload?.detail?.code === "PROJECT_VERSION_CONFLICT") await loadFormalReview(projectId);
    else showToast(formalError(error));
  } finally {
    state.formalReviewBusy = false;
    validateFormalReviewControls();
  }
}

async function confirmFormalReviewFinal(event) {
  if (state.formalReview?.read_only) return;
  event.preventDefault();
  const projectId = formalRoute().projectId;
  const submittedNote = elements.formalReviewFinalNote.value;
  if (!projectId || state.formalReviewBusy || elements.formalReviewFinalSubmit.disabled) return;
  const review = state.formalReview;
  const epoch = state.formalReviewEpoch;
  const active = () => state.formalReviewEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "review";
  state.formalReviewBusy = true;
  validateFormalReviewControls();
  elements.formalReviewFinalSubmit.textContent = "正在确认…";
  try {
    const model = await api(formalReviewPath(projectId, "final"), {
      method: "POST",
      body: JSON.stringify({
        expected_project_version: state.formalReview.project_version,
        confirmed: true,
        ...(elements.formalReviewFinalNote.value.trim() ? { note: elements.formalReviewFinalNote.value.trim() } : {}),
        idempotency_key: formalRequestKey("review-final"),
      }),
    });
    if (!active() || state.formalReview !== review) return;
    renderFormalReview(model);
    if (elements.formalReviewFinalNote.value === submittedNote) state.uiInputBaselines.delete(elements.formalReviewFinalNote);
    elements.formalReviewState.focus?.({ preventScroll: true });
    showToast("整片人工确认已完成；尚未正式发布。 ");
  } catch (error) {
    if (!active()) return;
    if (error?.payload?.detail?.code === "PROJECT_VERSION_CONFLICT") await loadFormalReview(projectId);
    else showToast(formalError(error));
  } finally {
    if (state.formalReviewEpoch === epoch) state.formalReviewBusy = false;
    if (!active()) return;
    elements.formalReviewFinalSubmit.textContent = "完成整片确认";
    validateFormalReviewControls();
  }
}

function formalDownloadFilename(response, fallback = "Loomtake项目-脚本交付.xlsx") {
  const value = response.headers.get("content-disposition") || "";
  const encoded = value.match(/filename\*=utf-8''([^;]+)/i)?.[1];
  if (!encoded) return fallback;
  try {
    return decodeURIComponent(encoded);
  } catch (_error) {
    return fallback;
  }
}

async function downloadFormalExcel() {
  const projectId = formalRoute().projectId;
  const projectVersion = state.formalReview?.project_version;
  if (!projectId || !projectVersion || state.formalDeliveryBusy) return;
  state.formalDeliveryBusy = true;
  elements.formalDeliveryExcel.disabled = true;
  elements.formalDeliveryPdf.disabled = true;
  elements.formalDeliveryExcel.textContent = "正在准备…";
  elements.formalDeliveryStatus.textContent = "正在复核当前版本和交付文件…";
  try {
    const query = new URLSearchParams({
      workspace_id: state.agentWorkspaceId,
      expected_project_version: String(projectVersion),
    });
    if (state.formalReview?.read_only) query.set("version_id", state.formalReview.base_version_id);
    const response = await fetch(
      `/api/formal/projects/${encodeURIComponent(projectId)}/review/excel?${query}`,
      { headers: { "X-MPlus-Agent-Auth": state.agentAuth }, cache: "no-store" },
    );
    if (!response.ok) {
      const payload = (response.headers.get("content-type") || "").includes("application/json")
        ? await response.json() : null;
      const error = new Error(payload?.detail?.message || "交付文件没有生成");
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = formalDownloadFilename(response);
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    elements.formalDeliveryStatus.textContent = "Excel 已导出，可在本机独立打开。";
    elements.formalDeliveryStatus.focus?.({ preventScroll: true });
  } catch (error) {
    elements.formalDeliveryStatus.textContent = formalError(error);
    elements.formalDeliveryStatus.focus?.({ preventScroll: true });
  } finally {
    state.formalDeliveryBusy = false;
    elements.formalDeliveryExcel.textContent = "导出 Excel";
    elements.formalDeliveryExcel.disabled = state.formalReview?.state !== "confirmed";
    elements.formalDeliveryPdf.disabled = state.formalReview?.state !== "confirmed";
  }
}

async function downloadFormalPdf() {
  const projectId = formalRoute().projectId;
  const projectVersion = state.formalReview?.project_version;
  if (!projectId || !projectVersion || state.formalDeliveryBusy) return;
  state.formalDeliveryBusy = true;
  elements.formalDeliveryExcel.disabled = true;
  elements.formalDeliveryPdf.disabled = true;
  elements.formalDeliveryPdf.textContent = "正在准备…";
  elements.formalDeliveryStatus.textContent = "正在从同一已确认版本生成可选 PDF…";
  try {
    const query = new URLSearchParams({
      workspace_id: state.agentWorkspaceId,
      expected_project_version: String(projectVersion),
    });
    if (state.formalReview?.read_only) query.set("version_id", state.formalReview.base_version_id);
    const response = await fetch(
      `/api/formal/projects/${encodeURIComponent(projectId)}/review/pdf?${query}`,
      { headers: { "X-MPlus-Agent-Auth": state.agentAuth }, cache: "no-store" },
    );
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok) {
      const payload = contentType.includes("application/json") ? await response.json() : null;
      const error = new Error(payload?.detail?.message || "PDF 没有生成");
      error.payload = payload;
      throw error;
    }
    if (contentType.includes("application/json")) {
      const payload = await response.json();
      elements.formalDeliveryStatus.textContent = `PDF 可选不可用：${payload.message || "当前生成器不可用"}；Excel 仍可正常导出。`;
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = formalDownloadFilename(response, "Loomtake项目-脚本交付.pdf");
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    elements.formalDeliveryStatus.textContent = "PDF 已从与 Excel 相同的交付 snapshot 导出。";
  } catch (error) {
    elements.formalDeliveryStatus.textContent = formalError(error);
  } finally {
    state.formalDeliveryBusy = false;
    elements.formalDeliveryPdf.textContent = "导出 PDF（可选）";
    elements.formalDeliveryExcel.disabled = state.formalReview?.state !== "confirmed";
    elements.formalDeliveryPdf.disabled = state.formalReview?.state !== "confirmed";
    elements.formalDeliveryStatus.focus?.({ preventScroll: true });
  }
}

function formalTaskCard(item) {
  const action = item.action_href && item.action_label
    ? `<a class="secondary-button compact" href="${escapeHtml(item.action_href)}">${escapeHtml(item.action_label)}</a>`
    : "";
  const issue = item.issue ? `<p class="ui00-task-issue">${escapeHtml(item.issue)}</p>` : "";
  const project = item.project_name ? `<small>${escapeHtml(item.project_name)}</small>` : "";
  return `<article class="ui00-task-card" data-state="${escapeHtml(item.state || "unknown")}"><header><div><span>${escapeHtml(item.state_label || "进行中")}</span><h2>${escapeHtml(item.title || "项目任务")}</h2>${project}</div><time datetime="${escapeHtml(item.updated_at || "")}">${escapeHtml(formalScriptDate(item.updated_at))}</time></header><p>${escapeHtml(item.next_action || "请刷新后查看最新状态。")}</p>${issue}${action}</article>`;
}

const workspaceTaskLabels = {
  received: "已接收", understanding: "理解中", needs_clarification: "需要补充信息",
  proposed: "待核对", waiting_confirmation: "待确认", executing: "进行中",
  waiting_human_review: "等待人工审核", waiting_user: "等待用户",
  completed: "已完成", partially_completed: "部分完成", blocked: "暂时无法继续",
  failed: "失败", cancelled: "已取消",
};

const workspaceTaskTypeLabels = {
  formal_generation_plan: "规划与脚本生成",
  formal_production_advisor: "项目创作助手",
  formal_stage_retry: "阶段重试",
  formal_script_revision: "脚本修改子版本",
  formal_image_revision: "图片反馈子版本",
  formal_image_version: "逐镜图片子版本",
  formal_default_image_adoption: "同类型创意库图片采用",
  formal_historical_continuation: "历史 Brief 续作",
  formal_historical_review_import: "历史人工复核导入",
  get_creative_strategies: "创作策略读取",
};

function workspaceTaskCard(item) {
  const status = item.status || item.state || "unknown";
  const historyQuery = new URLSearchParams(formalRoute().page === "tasks" ? window.location.search : "");
  historyQuery.delete("focus_run_id");
  historyQuery.set("view", "runs"); historyQuery.set("run_id", item.run_id || "");
  let actionHref = item.action_href || (item.run_id ? `/tasks?${historyQuery}` : "");
  if (actionHref && !/^\/(projects\/|tasks(?:\?|$))/.test(actionHref)) actionHref = "";
  if (item.task_id && actionHref.includes("/script")) {
    const url = new URL(actionHref, window.location.origin);
    url.searchParams.set("task_id", item.task_id);
    actionHref = `${url.pathname}${url.search}`;
  }
  return `<article class="task-compact-row"${item.run_id ? ` data-history-list-run="${escapeHtml(item.run_id)}"` : ""}><div class="task-identity"><strong>${escapeHtml(item.project_name || "未关联项目")}</strong><span>${escapeHtml(item.title || (item.run_id ? "历史运行" : workspaceTaskTypeLabels[item.task_type] || "项目任务"))}</span></div><span class="task-state" data-state="${escapeHtml(status)}">${escapeHtml(item.state_label || workspaceTaskLabels[status] || "状态待读取")}</span><time>${escapeHtml(taskDisplayTime(item.updated_at))}</time><div class="task-next"><p>${escapeHtml(item.issue || item.next_action || item.message || "请查看项目当前状态。")}</p>${actionHref ? `<a class="secondary-button compact" data-list-page href="${escapeHtml(actionHref)}">${escapeHtml(item.action_label || "查看记录")}</a>` : ""}</div></article>`;
}

function taskDisplayTime(value) {
  if (!value) return "时间未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未记录" : date.toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
}

function taskQueryParams(query = new URLSearchParams(window.location.search)) {
  const params = new URLSearchParams({page_size: "50"});
  for (const key of ["project_id", "status", "query", "created_from", "created_to", "cursor"]) if (query.get(key)) params.set(key, query.get(key));
  return params;
}

function taskScopeCopy(response) {
  const count = Number(response.count ?? response.items?.length ?? response.tasks?.length ?? 0);
  const total = response.matched_total;
  return total == null ? `本次返回 ${count} 条；接口未提供全量数量。` : `本页 ${count} 条，筛选匹配共 ${total} 条。按${response.sort_field === "updated_at" ? "更新时间" : "创建时间"}从新到旧排列；刷新纳入新增和状态变化，不是历史快照。`;
}

function renderTaskPages(selector, response, path) {
  const panel = document.querySelector(selector);
  const query = new URLSearchParams(window.location.search);
  query.delete("task_id");
  const first = new URLSearchParams(query); first.delete("cursor");
  const next = new URLSearchParams(query); if (response.next_cursor) next.set("cursor", response.next_cursor);
  panel.innerHTML = `${query.has("cursor") ? `<a class="secondary-button compact" data-list-page href="${escapeHtml(`${path}?${first}`)}">返回第一页</a>` : ""}${response.next_cursor ? `<a class="secondary-button compact" data-list-page href="${escapeHtml(`${path}?${next}`)}">下一页</a>` : '<span>已到本次查询末页</span>'}`;
}

async function loadHomeTasks() {
  const target = document.querySelector("#home-task-summary"), epoch = ++state.homeTaskEpoch;
  const workspace = state.agentWorkspaceId;
  const active = () => state.homeTaskEpoch === epoch && workspace === state.agentWorkspaceId && formalRoute().page === "projects";
  if (!state.agentAuth || !workspace) { target.textContent = "进入工作区后读取任务。"; return; }
  target.textContent = "正在读取当前待办…";
  const status = "received,understanding,needs_clarification,proposed,waiting_confirmation,executing,waiting_human_review,waiting_user,partially_completed,blocked,failed";
  try {
    const result = await api(`/api/formal/workspaces/${encodeURIComponent(workspace)}/tasks?${new URLSearchParams({page_size:"5",status})}`);
    if (!active()) return;
    const items = result.items || [];
    target.innerHTML = items.length ? `<p>${result.matched_total == null ? "本次读取的待办" : `工作区待办 ${result.matched_total} 项`}</p>${items.map(workspaceTaskCard).join("")}<a class="text-button" data-list-page href="/tasks?${escapeHtml(new URLSearchParams({status}).toString())}">查看全部待办</a>` : '<div class="workbench-empty">当前筛选没有待处理任务。</div>';
  } catch (error) {
    if (active()) target.innerHTML = `<div class="workbench-empty"><strong>任务读取失败</strong><p>${escapeHtml(formalError(error))}</p><button class="secondary-button compact" data-home-task-retry>重试读取</button></div>`;
  }
}

function renderHistoricalRunDetail(item) {
  const returnQuery = new URLSearchParams(window.location.search);
  returnQuery.delete("run_id"); returnQuery.set("view", "runs");
  returnQuery.set("focus_run_id", item.run_id);
  const brief = Object.entries(item.brief || {}).map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(Array.isArray(value) ? value.join("、") : value)}</dd></div>`).join("");
  const artifacts = (item.artifacts || []).map((artifact) => `<li><button class="secondary-button compact" type="button" data-history-artifact="${escapeHtml(artifact.download_url)}" data-history-filename="${escapeHtml(artifact.filename)}">${escapeHtml(artifact.label)} · ${escapeHtml(artifact.filename)}</button></li>`).join("");
  const actions = item.actions || {};
  const reviewImportReason = actions.review_import ? "" : `<p data-history-review-import-reason>${escapeHtml(actions.review_import_reason || "当前状态不能导入人工复核结果。")}</p>`;
  const projectPath = `/projects/${encodeURIComponent(item.project_id)}`;
  return `<article class="ui00-business-empty" data-history-run-detail="${escapeHtml(item.run_id)}" data-history-project="${escapeHtml(item.project_id)}"><a href="/tasks?${escapeHtml(returnQuery.toString())}" data-list-page>返回历史运行</a><h2>${escapeHtml(item.project_name)}</h2><p>${escapeHtml(item.message || item.state)}</p><h3>当次 Brief</h3><dl class="ui00-image-facts">${brief || "<div><dd>没有允许展示的 Brief 字段</dd></div>"}</dl><h3>允许访问的文件</h3><ul>${artifacts || "<li>当前没有可访问文件</li>"}</ul><h3>需要修改</h3><p>历史详情只读。脚本、事实或图片修改请回到当前项目，避免从旧运行记录创建平行修改入口。</p><div class="ui00-inline-actions"><a class="secondary-button compact" href="${projectPath}/script" data-history-edit="script">前往脚本与版本</a><a class="secondary-button compact" href="${projectPath}/shot-images" data-history-edit="shot-images">前往逐镜配图</a></div><section class="ui00-image-version-message" data-history-revision-preview hidden tabindex="-1"></section><h3>历史续作</h3><div class="ui00-inline-actions"><label class="secondary-button compact${actions.review_import ? "" : " is-disabled"}"${actions.review_import ? "" : ` title="${escapeHtml(actions.review_import_reason || "当前状态不能导入")}"`}>导入人工复核结果<input type="file" accept=".xlsx" data-history-review-file hidden${actions.review_import ? "" : " disabled"} /></label><button class="secondary-button compact" type="button" data-history-continue="current"${actions.continue_from_brief ? "" : " disabled"}>从历史 Brief 续作当前项目</button><button class="secondary-button compact" type="button" data-history-continue="new"${actions.continue_from_brief ? "" : " disabled"}>从历史需求新建项目</button></div>${reviewImportReason}<p>历史续作和复核导入都会先显示影响，确认前不会写入。</p></article>`;
}

async function previewHistoricalContinuation(button) {
  const detail = button.closest("[data-history-run-detail]");
  const preview = detail.querySelector("[data-history-revision-preview]");
  const action = button.dataset.historyContinue === "new" ? "new_project" : "current_project";
  preview.hidden = false;
  preview.textContent = "正在预览历史 Brief 续作影响…";
  try {
    const result = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/legacy-runs/${encodeURIComponent(detail.dataset.historyRunDetail)}/continuation-proposals`, {
      method: "POST",
      body: JSON.stringify({ action, idempotency_key: formalRequestKey("history-continuation") }),
    });
    state.historicalContinuationProposal = result;
    preview.innerHTML = `<strong>确认继续使用历史需求</strong><p>目标：${escapeHtml(result.after?.target || "继续使用历史需求")}</p><p>影响字段：${escapeHtml((result.affected_fields || []).join("、"))}</p><p>将重跑：${escapeHtml((result.rerun_nodes || []).join("、"))}</p><button class="primary-button compact" type="button" data-confirm-history-continuation="1">确认创建后续项目需求</button>`;
    preview.focus({ preventScroll: true });
  } catch (error) {
    preview.innerHTML = `<strong>历史 Brief 续作提案失败</strong><p>${escapeHtml(formalError(error))}</p>`;
  }
}

async function confirmHistoricalContinuation(button) {
  const detail = button.closest("[data-history-run-detail]");
  const pending = state.historicalContinuationProposal;
  if (!pending?.proposal_id || !pending?.proposal_digest) return;
  button.disabled = true;
  try {
    const result = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/legacy-runs/${encodeURIComponent(detail.dataset.historyRunDetail)}/continuation-proposals/${encodeURIComponent(pending.proposal_id)}/confirm`, {
      method: "POST", body: JSON.stringify({ proposal_digest: pending.proposal_digest }),
    });
    state.historicalContinuationProposal = null;
    detail.querySelector("[data-history-revision-preview]").innerHTML = `<strong>已创建后续项目需求</strong><p>新需求仍需人工确认，不会自动生成或交付。</p><a class="primary-button compact" href="${escapeHtml(result.next_href)}">进入项目需求</a>`;
  } catch (error) {
    detail.querySelector("[data-history-revision-preview]").innerHTML = `<strong>未能继续使用历史需求</strong><p>${escapeHtml(formalError(error))}</p>`;
  }
}

async function previewHistoricalReviewImport(input) {
  const detail = input.closest("[data-history-run-detail]");
  const preview = detail.querySelector("[data-history-revision-preview]");
  const file = input.files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file, file.name);
  preview.hidden = false;
  preview.textContent = "正在核验复核表来源、不可编辑内容和审核覆盖…";
  try {
    const result = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/legacy-runs/${encodeURIComponent(detail.dataset.historyRunDetail)}/review-import-proposals`, {
      method: "POST", body: form,
    });
    state.historicalReviewProposal = result;
    preview.innerHTML = `<strong>请确认导入人工复核结果</strong><p>审核状态：${escapeHtml(result.after?.human_review_status || "待确认")}</p><p>问题数：${escapeHtml(String(result.after?.issue_count || 0))}</p><p>只影响：人工审核决定；不会修改脚本、图片或历史产物。</p><button class="primary-button compact" type="button" data-confirm-history-review="1">确认导入复核决定</button>`;
    preview.focus({ preventScroll: true });
  } catch (error) {
    preview.innerHTML = `<strong>复核表未形成可确认提案</strong><p>${escapeHtml(formalError(error))}</p>`;
  } finally {
    input.value = "";
  }
}

async function confirmHistoricalReviewImport(button) {
  const detail = button.closest("[data-history-run-detail]");
  const pending = state.historicalReviewProposal;
  if (!pending?.proposal_id || !pending?.proposal_digest) return;
  button.disabled = true;
  try {
    const result = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/legacy-runs/${encodeURIComponent(detail.dataset.historyRunDetail)}/review-import-proposals/${encodeURIComponent(pending.proposal_id)}/confirm`, {
      method: "POST", body: JSON.stringify({ proposal_digest: pending.proposal_digest }),
    });
    state.historicalReviewProposal = null;
    detail.querySelector("[data-history-revision-preview]").textContent = `人工复核结果已导入；当前审核状态：${result.review_state}。`;
  } catch (error) {
    detail.querySelector("[data-history-revision-preview]").innerHTML = `<strong>人工复核结果未导入</strong><p>${escapeHtml(formalError(error))}</p>`;
  }
}

async function downloadHistoricalArtifact(button) {
  button.disabled = true;
  try {
    const response = await fetch(button.dataset.historyArtifact, {
      headers: { "X-MPlus-Agent-Auth": state.agentAuth }, cache: "no-store",
    });
    if (!response.ok) {
      const payload = (response.headers.get("content-type") || "").includes("application/json")
        ? await response.json() : null;
      throw { payload, status: response.status };
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = button.dataset.historyFilename || "历史产物";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (error) {
    showToast(formalError(error));
  } finally {
    button.disabled = false;
  }
}

function populateTaskFilters() {
  if (state.taskFilterUrl === window.location.search) return;
  state.taskFilterUrl = window.location.search;
  const query = new URLSearchParams(window.location.search);
  const current = query.get("project_id") || "";
  elements.formalTaskProjectFilter.innerHTML = '<option value="">全部项目</option>' + state.formalProjects.map((project) => `<option value="${escapeHtml(project.project_id)}">${escapeHtml(project.name)}</option>`).join("");
  if (state.formalProjects.some((project) => project.project_id === current)) elements.formalTaskProjectFilter.value = current;
  elements.formalTaskStatusFilter.value = query.get("status") || "";
  for (const [id, key] of [["formal-task-query", "query"], ["formal-task-from", "created_from"], ["formal-task-to", "created_to"]]) document.getElementById(id).value = query.get(key) || "";
}

async function loadWorkspaceTasks() {
  const epoch = ++state.formalTaskEpoch, location = window.location.pathname + window.location.search;
  const active = () => epoch === state.formalTaskEpoch && location === window.location.pathname + window.location.search && formalRoute().page === "tasks";
  const available = Boolean(state.agentAuth && state.agentWorkspaceId);
  elements.formalTaskProjectRequired.hidden = available;
  elements.formalTaskList.hidden = !available;
  elements.formalTaskCount.textContent = available ? "读取中" : "等待工作区";
  if (!available) return;
  populateTaskFilters();
  const query = new URLSearchParams(window.location.search);
  const view = query.get("view") === "runs" ? "runs" : "tasks";
  document.querySelectorAll("[data-task-view]").forEach((tab) => {
    const selected = tab.dataset.taskView === view;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-selected", String(selected));
  });
  elements.formalTaskList.innerHTML = '<div class="ui00-business-empty"><strong>正在读取工作区记录</strong><p>请稍候。</p></div>';
  try {
    const params = taskQueryParams();
    if (query.get("project_id")) params.set("project_id", query.get("project_id"));
    if (query.get("status")) params.set("status", query.get("status"));
    const endpoint = view === "runs" ? "legacy-runs" : "tasks";
    const selectedRunId = view === "runs" ? query.get("run_id") : "";
    if (selectedRunId) {
      const detail = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/legacy-runs/${encodeURIComponent(selectedRunId)}`);
      if (!active()) return;
      document.querySelector("#formal-task-pages").innerHTML = "";
      document.querySelector("#formal-task-scope").textContent = "当前历史运行详情";
      elements.formalTaskCount.textContent = "历史详情";
      elements.formalTaskList.innerHTML = renderHistoricalRunDetail(detail);
      const reviewFile = elements.formalTaskList.querySelector("[data-history-review-file]");
      const reviewControl = reviewFile?.closest("label");
      if (reviewControl) {
        reviewControl.dataset.historyReviewControl = "1";
        reviewControl.tabIndex = reviewFile.disabled ? -1 : 0;
        reviewControl.setAttribute("role", "button");
        reviewControl.setAttribute("aria-disabled", reviewFile.disabled ? "true" : "false");
        reviewFile.hidden = false;
        reviewFile.classList.add("visually-hidden");
      }
      return;
    }
    const response = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/${endpoint}?${params}`);
    if (!active()) return;
    state.formalTaskPage = response;
    document.querySelector("#formal-task-scope").textContent = taskScopeCopy(response);
    renderTaskPages("#formal-task-pages", response, "/tasks");
    const items = Array.isArray(response?.items) ? response.items : [];
    elements.formalTaskCount.textContent = response.matched_total == null ? `本次 ${items.length} 项` : `匹配 ${response.matched_total} 项`;
    elements.formalTaskList.innerHTML = items.length
      ? items.map(workspaceTaskCard).join("")
      : `<div class="ui00-business-empty"><strong>暂无${view === "runs" ? "历史运行" : "业务任务"}</strong><p>满足筛选条件的记录会显示在这里。</p></div>`;
    if (view === "runs" && query.get("focus_run_id")) {
      const row = [...elements.formalTaskList.querySelectorAll("[data-history-list-run]")]
        .find(item => item.dataset.historyListRun === query.get("focus_run_id"));
      row?.scrollIntoView({ block: "center" });
      row?.querySelector("a")?.focus({ preventScroll: true });
    }
  } catch (error) {
    if (!active()) return;
    document.querySelector("#formal-task-pages").innerHTML = "";
    elements.formalTaskCount.textContent = "读取失败";
    const dateError = error?.payload?.detail?.code === "INVALID_INPUT" && /日期/.test(error?.payload?.detail?.message || "");
    const message = dateError ? "请核对创建日期范围：开始日期不能晚于结束日期，调整后重新应用筛选。" : formalError(error);
    elements.formalTaskList.innerHTML = `<div class="ui00-business-empty"><strong>${dateError ? "请调整日期筛选" : "任务状态暂不可用"}</strong><p>${escapeHtml(message)}</p><button class="secondary-button compact" data-task-refresh>重新读取</button></div>`;
  }
}

async function loadUnifiedSystem() {
  if (!state.agentAuth || !state.agentWorkspaceId) {
    elements.systemGrid.innerHTML = '<div class="ui00-business-empty"><strong>工作区身份不可用</strong><p>请先返回项目列表进入本机工作区。</p></div>';
    return;
  }
  try {
    const result = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/system`);
    state.unifiedSystem = result;
    renderSystemChecks(result.checks || []);
    const note = document.querySelector("#page-system .system-note");
    if (note) note.innerHTML = `<span>当前状态</span><strong>${escapeHtml(result.summary || "状态已读取")}</strong><p>${Number(result.attention_count || 0) ? "请处理上方异常项后再继续相关操作。" : "这里仅显示本机运行依赖；项目内容请在对应项目内检查。"}</p>`;
  } catch (error) {
    elements.systemGrid.innerHTML = `<div class="ui00-business-empty"><strong>系统状态暂不可用</strong><p>${escapeHtml(formalError(error))}</p></div>`;
  }
}

async function loadFormalAdminAccess() {
  state.formalAdminAccess = false;
  if (elements.formalAdminLink) elements.formalAdminLink.hidden = true;
  if (!state.agentAuth || !state.agentWorkspaceId) return;
  try {
    const result = await api(`/api/formal/admin/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/access`);
    state.formalAdminAccess = result?.admin_console === "available";
  } catch (_error) {
    state.formalAdminAccess = false;
  }
  if (elements.formalAdminLink) elements.formalAdminLink.hidden = !state.formalAdminAccess;
}

function renderFormalAdminOverview(result) {
  const services = result?.services || {};
  elements.formalAdminSchema.textContent = String(result?.schema_version ?? "—");
  elements.formalAdminOutbox.textContent = String(services.outbox_pending ?? "—");
  elements.formalAdminDeadletters.textContent = String(services.dead_letter ?? "—");
  const tasks = Array.isArray(result?.tasks) ? result.tasks : [];
  elements.formalAdminTaskCount.textContent = result.matched_total == null ? `本次 ${tasks.length} 项` : `匹配 ${result.matched_total} 项`;
  document.querySelector("#admin-task-scope").textContent = taskScopeCopy(result);
  const scroll = elements.formalAdminTaskList.scrollTop;
  elements.formalAdminTaskList.innerHTML = tasks.length ? tasks.map(task => `<button class="admin-task-row" type="button" data-admin-task="${escapeHtml(task.task_id)}" aria-current="${task.task_id === state.formalAdminSelected}"><strong>${escapeHtml(task.project_name || "未命名项目")}</strong><span>${escapeHtml(task.title || "项目任务")} · ${escapeHtml(task.state_label || agentStatusLabel(task.status))}</span><small>${escapeHtml(task.issue || task.next_action || "查看详情")} · ${escapeHtml(taskDisplayTime(task.updated_at))}</small></button>`).join("") : '<div class="workbench-empty">当前筛选没有匹配任务。</div>';
  elements.formalAdminTaskList.scrollTop = scroll;
  renderTaskPages("#admin-task-pages", result, "/admin");
  const operations = Array.isArray(result?.operations) ? result.operations : [];
  elements.formalAdminOperations.innerHTML = operations.length ? operations.map(item => `<article class="ui00-project-row"><div><strong>${escapeHtml(item.label || "运维记录")} · ${escapeHtml(item.status_label || item.status || "状态未记录")}</strong><p>${escapeHtml(item.summary || "暂无说明")}</p><small>${escapeHtml(taskDisplayTime(item.created_at))}</small></div></article>`).join("") : '<div class="workbench-empty">暂无记录。</div>';
}

function recoveryStatusLabel(status) {
  return {created:"已创建", executing:"执行中", succeeded:"执行成功", completed:"已完成", failed:"执行失败", blocked:"待业务处理", waiting_user:"待业务确认", waiting_human_review:"待人工审核", unknown:"结果尚未确认"}[status] || "结果尚未确认";
}

function renderFormalAdminTask(detail) {
  const task = detail?.task || {};
  const attempts = detail?.attempts || [];
  const providers = detail?.provider_calls || [];
  const actions = detail?.recovery_actions || [];
  const evidence = providers.map(item => `<li>${escapeHtml(item.provider)} · ${escapeHtml(recoveryStatusLabel(item.status))} · ${escapeHtml(item.duration_ms ?? "—")} ms · ${escapeHtml(item.error_code || "无错误码")}<p>${escapeHtml(item.error_summary || "未记录错误说明")}</p></li>`).join("");
  const attemptsHtml = attempts.map(item => `<article class="attempt-result"><strong>第 ${escapeHtml(item.attempt_no)} 次尝试 · ${escapeHtml(item.state_label || recoveryStatusLabel(item.execution_status))}</strong><p>${escapeHtml(item.summary || "结果尚未读取")}</p><small>${escapeHtml(taskDisplayTime(item.created_at))}</small></article>`).join("");
  const checkpoints = (detail?.checkpoints || []).filter(item => item.recoverable);
  elements.formalAdminTaskDetail.innerHTML = `<article data-admin-task-context="${escapeHtml(task.task_id || "")}"><h2 tabindex="-1">${escapeHtml(task.project_name || "任务详情")}</h2><p>${escapeHtml(task.title || "项目任务")} · ${escapeHtml(task.state_label || agentStatusLabel(task.status))}</p><h3>原因与影响</h3><p>${escapeHtml(task.issue || detail.summary || "未记录可展示的问题说明，请核对当前项目状态。")}</p><p>${escapeHtml(detail.impact || "原任务记录保留，当前项目内容以已保存版本为准。")}</p><h3>已完成内容与新尝试</h3>${attemptsHtml || '<p>暂无尝试结果记录。</p>'}<h3>下一步</h3><p>${escapeHtml(detail.next_action || detail.recovery_unavailable_reason || "查看当前项目")}</p>${actions.length ? `<form data-admin-recovery-form="1"><label><span>恢复方式</span><select name="action">${actions.map(item => `<option value="${escapeHtml(item.action)}">${escapeHtml(item.label)}</option>`).join("")}</select></label><label><span>当前任务检查点</span><select name="checkpoint_id"><option value="">使用当前阶段</option>${checkpoints.map(item => `<option value="${escapeHtml(item.checkpoint_id)}">${escapeHtml(item.stage_label || "已记录阶段")} · ${escapeHtml(recoveryStatusLabel(item.status))}</option>`).join("")}</select></label><label><span>恢复原因</span><textarea name="reason" rows="3" maxlength="1000" required placeholder="说明已核对的问题及本次恢复原因"></textarea></label><button class="primary-button compact" type="submit">预览恢复影响</button></form><section data-admin-recovery-proposal hidden tabindex="-1"></section>` : ""}<details><summary>技术证据</summary><ul>${evidence || '<li>暂无外部调用记录。</li>'}</ul><p>任务事件 ${Number(detail.events?.length || 0)} 条，检查点 ${Number(detail.checkpoints?.length || 0)} 条。</p><ol>${(detail.events || []).map(event => `<li>${escapeHtml(taskDisplayTime(event.created_at))} · ${escapeHtml(recoveryStatusLabel(event.status))} · ${escapeHtml(event.stage_id || "未记录阶段")} · ${escapeHtml(event.error_code || "无错误码")}</li>`).join("")}</ol></details><button class="text-button" data-admin-detail-retry="${escapeHtml(task.task_id)}">刷新本任务结果</button></article>`;
}

function adminSection() {
  const section = new URLSearchParams(window.location.search).get("section") || "tasks";
  return ["tasks", "system", "approvals", "operations"].includes(section) ? section : "tasks";
}

async function loadFormalAdmin({preserveDetails = true} = {}) {
  const epoch = ++state.formalAdminEpoch;
  const active = () => epoch === state.formalAdminEpoch && formalRoute().page === "admin";
  elements.formalAdminBlocked.hidden = state.formalAdminAccess;
  elements.formalAdminConsole.hidden = !state.formalAdminAccess;
  if (!state.formalAdminAccess) return;
  const section = adminSection(), params = new URLSearchParams(window.location.search);
  document.querySelectorAll("[data-admin-panel]").forEach(panel => { panel.hidden = panel.dataset.adminPanel !== section; });
  document.querySelectorAll("[data-admin-section]").forEach(link => link.setAttribute("aria-current", String(link.dataset.adminSection === section)));
  const form = document.querySelector("#admin-task-filters");
  if (state.adminFilterUrl !== window.location.search) {
    form.elements.project_id.innerHTML = '<option value="">全部项目</option>' + state.formalProjects.map(p => `<option value="${escapeHtml(p.project_id)}">${escapeHtml(p.name)}</option>`).join("");
    for (const key of ["query", "project_id", "status", "created_from", "created_to"]) form.elements[key].value = params.get(key) || "";
    state.adminFilterUrl = window.location.search;
  }
  elements.formalAdminTaskCount.textContent = "读取中";
  try {
    const result = await api(`/api/formal/admin/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/overview?${taskQueryParams(params)}`);
    if (!active()) return;
    state.formalAdminOverview = result;
    state.formalAdminSelected = params.get("task_id") || "";
    renderFormalAdminOverview(result);
    const selected = state.formalAdminSelected;
    if (section === "tasks" && selected) {
      const dirty = elements.formalAdminTaskDetail.querySelector('textarea[name="reason"]')?.value;
      if (!(preserveDetails && dirty && state.formalAdminTask?.task?.task_id === selected)) await loadFormalAdminTask(selected, {history: false, focus: false});
    }
    if (section === "system") await loadAdminVerification();
    if (section === "approvals") await loadAdminApprovals();
  } catch (error) {
    if (!active()) return;
    elements.formalAdminTaskCount.textContent = "读取失败";
    elements.formalAdminTaskList.innerHTML = `<div class="workbench-empty"><strong>${error.status === 403 ? "没有读取权限" : "任务读取失败"}</strong><p>${escapeHtml(formalError(error))}</p><button class="secondary-button compact" data-admin-list-retry>重新读取</button></div>`;
    document.querySelector("#admin-task-scope").textContent = "本次读取失败，旧详情不代表最新结果。";
  }
}

async function loadFormalAdminTask(taskId, {history = true, focus = true} = {}) {
  const epoch = ++state.formalAdminDetailEpoch;
  state.formalAdminSelected = taskId;
  state.formalAdminTask = null;
  state.formalAdminProposal = null;
  const active = () => formalRoute().page === "admin" && state.formalAdminSelected === taskId && epoch === state.formalAdminDetailEpoch;
  if (history) {
    const url = new URL(window.location.href); url.searchParams.set("task_id", taskId);
    window.history.pushState({}, "", `${url.pathname}${url.search}`);
  }
  elements.formalAdminTaskList.querySelectorAll("[data-admin-task]").forEach(button => button.setAttribute("aria-current", String(button.dataset.adminTask === taskId)));
  document.querySelector("#admin-task-layout").classList.add("has-selection");
  document.querySelector("#page-admin").classList.add("has-task-detail");
  elements.formalAdminTaskDetail.innerHTML = '<h2 tabindex="-1">正在读取任务详情…</h2>';
  if (focus) elements.formalAdminTaskDetail.querySelector("h2")?.focus({preventScroll:true});
  try {
    const detail = await api(`/api/formal/admin/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/tasks/${encodeURIComponent(taskId)}`);
    if (!active()) return;
    state.formalAdminTask = detail;
    renderFormalAdminTask(detail);
    if (focus) elements.formalAdminTaskDetail.querySelector("h2")?.focus({preventScroll:true});
  } catch (error) {
    if (!active()) return;
    elements.formalAdminTaskDetail.innerHTML = `<h2 tabindex="-1">${error.status === 404 ? "所选任务已不可读取" : error.status === 403 ? "没有读取权限" : "任务详情读取失败"}</h2><p>${escapeHtml(formalError(error))}</p><button class="secondary-button compact" data-admin-detail-retry="${escapeHtml(taskId)}">重新读取本任务</button>`;
  }
}

async function previewFormalAdminRecovery(form) {
  const task = state.formalAdminTask?.task;
  if (!task || state.formalAdminRecoveryBusy) return;
  const epoch = state.formalAdminDetailEpoch;
  const active = () => state.formalAdminSelected === task.task_id && epoch === state.formalAdminDetailEpoch && formalRoute().page === "admin";
  const data = new FormData(form), action = String(data.get("action")), checkpointId = String(data.get("checkpoint_id") || ""), reason = String(data.get("reason") || "").trim();
  if (!reason) { form.querySelector("textarea").focus(); return; }
  if (action === "resume_checkpoint" && !checkpointId) { showToast("请选择当前任务的检查点"); return; }
  const values = {action, reason, checkpoint_id: action === "resume_checkpoint" ? checkpointId : "", expected_project_version:Number(task.project_version)};
  const fingerprint = JSON.stringify([task.task_id, values]);
  if (state.formalAdminIntent?.fingerprint !== fingerprint) state.formalAdminIntent = {fingerprint, key: crypto.randomUUID()};
  const intent = state.formalAdminIntent;
  state.formalAdminRecoveryBusy = true;
  form.querySelector('button[type="submit"]').disabled = true;
  try {
    const proposal = await api(`/api/formal/admin/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/tasks/${encodeURIComponent(task.task_id)}/recovery-proposals`, {method:"POST", body:JSON.stringify({...values, idempotency_key:intent.key})});
    if (!active() || state.formalAdminIntent !== intent) return;
    state.formalAdminProposal = proposal;
    const panel = form.parentElement.querySelector("[data-admin-recovery-proposal]");
    panel.hidden = false;
    panel.innerHTML = `<h3>恢复影响预览</h3><p>目标：${escapeHtml(task.project_name || "当前项目")}，第 ${Number(proposal.before?.attempt_no || 0) + 1} 次尝试。</p><p>${escapeHtml(proposal.stage_label || "按当前任务记录的阶段继续")}。${escapeHtml(proposal.will_change || "将创建新尝试，旧任务历史保留。")}</p><label><input type="checkbox" data-admin-recovery-check /> 已核对目标、阶段与影响</label><div class="ui00-inline-actions"><button class="primary-button compact" type="button" data-admin-recovery-confirm disabled>确认恢复</button><button class="secondary-button compact" type="button" data-admin-recovery-cancel>取消</button></div>`;
    panel.focus({preventScroll:true});
  } finally {
    state.formalAdminRecoveryBusy = false;
    if (active()) form.querySelector('button[type="submit"]').disabled = false;
  }
}

async function confirmFormalAdminRecovery() {
  const proposal = state.formalAdminProposal, taskId = state.formalAdminSelected;
  const epoch = state.formalAdminDetailEpoch;
  const active = () => formalRoute().page === "admin" && state.formalAdminSelected === taskId && state.formalAdminDetailEpoch === epoch;
  if (!proposal || state.formalAdminRecoveryBusy || !elements.formalAdminTaskDetail.querySelector("[data-admin-recovery-check]")?.checked) return;
  state.formalAdminRecoveryBusy = true;
  const button = elements.formalAdminTaskDetail.querySelector("[data-admin-recovery-confirm]");
  button.disabled = true; button.textContent = "正在读取恢复结果…";
  try {
    const result = await api(`/api/formal/admin/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/recovery-proposals/${encodeURIComponent(proposal.proposal_id)}/confirm`, {method:"POST", body:JSON.stringify({proposal_digest:proposal.proposal_digest})});
    if (!active()) return;
    state.formalAdminProposal = null;
    await loadFormalAdminTask(result.task_id || taskId, {history:false});
    showToast("已回读新尝试记录，请查看实际结果。");
  } catch (error) {
    if (active()) {
      button.disabled = false; button.textContent = "重读同一次恢复结果";
      showToast(formalError(error));
    }
  } finally { state.formalAdminRecoveryBusy = false; }
}

async function loadFormalCategories() {
  const epoch = ++state.formalCategoryEpoch;
  const selected = elements.formalProjectCategory.value;
  elements.formalProjectCategory.disabled = true;
  elements.formalCategoryRetry.hidden = true;
  elements.formalCategoryStatus.textContent = "正在读取类型…";
  const catalogCopy = document.querySelector("#formal-category-catalog");
  if (catalogCopy) catalogCopy.textContent = "正在读取可选项目类型…";
  try {
    const result = await api("/api/formal/categories");
    if (epoch !== state.formalCategoryEpoch) return;
    const items = (result.items || []).filter((item) => item.category_id && item.label);
    if (!items.length) throw new Error("暂无可用类型");
    state.formalCategories = items;
    if (catalogCopy) catalogCopy.textContent = `可选类型：${items.map(item => item.label).join("、")}。模板随项目类型提供。`;
    elements.formalProjectCategory.innerHTML = '<option value="">请选择类型</option>' + items.map((item) => `<option value="${escapeHtml(item.category_id)}">${escapeHtml(item.label)}</option>`).join("");
    elements.formalProjectCategory.value = items.some((item) => item.category_id === selected) ? selected : "";
    elements.formalProjectCategory.disabled = false;
    elements.formalCategoryStatus.textContent = "";
  } catch (_error) {
    if (epoch !== state.formalCategoryEpoch) return;
    state.formalCategories = [];
    if (catalogCopy) catalogCopy.textContent = "项目类型未能读取，请重试；已选模板保持不变。";
    elements.formalProjectCategory.innerHTML = '<option value="">类型读取失败</option>';
    elements.formalCategoryStatus.textContent = "类型暂未读到，可以重新读取。";
    elements.formalCategoryRetry.hidden = false;
  }
}

function syncFormalTemplateRoute() {
  const route = formalRoute();
  const key = `${route.projectId || ""}/${route.page}`;
  if (state.formalTemplateRoute === key) return;
  state.formalTemplateRoute = key;
  state.formalTemplateEpoch += 1;
  state.formalTemplates = null;
  state.formalTemplateRef = "";
  state.formalTemplateBusy = "";
  elements.formalTemplateFile.value = "";
  elements.formalTemplatePicker.open = route.page === "brief" && new URLSearchParams(window.location.search).get("panel") === "templates";
  elements.formalTemplateCurrent.textContent = "正在读取模板…";
  elements.formalPlanTemplate.textContent = "正在读取…";
  elements.formalTemplateSelect.innerHTML = "";
  elements.formalTemplatePreview.innerHTML = "";
  elements.formalTemplatePreview.hidden = true;
  invalidateFormalTemplateInput();
  renderFormalTemplateControls();
}

function formalTemplateIsCurrent(projectId, page, epoch) {
  const route = formalRoute();
  return route.projectId === projectId && formalSurface() === page && state.formalTemplateEpoch === epoch;
}

function formalTemplateLabel(item) {
  return item ? `${item.label || "未命名模板"} · v${item.version}` : "尚未选择模板";
}

function formalTemplateSelected(catalog = state.formalTemplates) {
  return catalog?.items?.find((item) => item.selected || item.ref === catalog.selected_ref)
    || (!catalog?.selected_ref ? catalog?.items?.find((item) => item.is_default) : undefined);
}

function invalidateFormalTemplateInput() {
  state.formalTemplateInputRevision += 1;
  state.formalTemplateTrial = null;
  elements.formalTemplateConfirm.checked = false;
  elements.formalTemplateTrialResult.hidden = true;
  elements.formalTemplateTrialResult.innerHTML = "";
}

function formalTemplateWritable() {
  return formalRoute().page === "brief" && state.formalTemplates?.project_id === formalRoute().projectId
    && Boolean(state.formalTemplates?.category_id)
    && !state.formalTemplates?.requires_brief_confirmation
    && formalProject()?.status !== "archived";
}

function renderFormalTemplateControls() {
  const catalog = state.formalTemplates;
  const item = catalog?.items?.find((entry) => entry.ref === state.formalTemplateRef);
  const busy = Boolean(state.formalTemplateBusy);
  const writable = formalTemplateWritable();
  const changed = Boolean(item && (catalog.is_bound === false || item.ref !== formalTemplateSelected(catalog)?.ref));
  elements.formalTemplateSelect.disabled = busy || !catalog?.items?.length;
  document.querySelector('#formal-template-category').disabled = busy || !catalog?.categories?.length;
  document.querySelector('#formal-template-category-confirm').hidden = !catalog?.requires_brief_confirmation;
  elements.formalTemplateFile.disabled = busy || !writable;
  elements.formalTemplateImport.disabled = busy || !writable || !elements.formalTemplateFile.files.length;
  elements.formalTemplateTrialButton.disabled = busy || !writable || !item || !catalog?.can_trial;
  elements.formalTemplateConfirm.disabled = busy || !writable || !changed;
  elements.formalTemplateApply.disabled = busy || !writable || !changed || !elements.formalTemplateConfirm.checked;
  elements.formalTemplateRetry.disabled = busy;
  elements.formalTemplateBinding.hidden = !changed || !writable;
  elements.formalTemplateBindingCopy.textContent = changed ? `后续生成使用「${formalTemplateLabel(item)}」。已保存脚本保持不变。` : "";
  elements.formalTemplateTrialHint.textContent = catalog?.can_trial ? "仅核对结构与镜头，不生成脚本版本。" : "确认需求后可试排，也可先选择模板。";
  elements.formalTemplateImport.textContent = state.formalTemplateBusy === "import" ? "正在核对…" : "导入并核对";
  elements.formalTemplateTrialButton.textContent = state.formalTemplateBusy === "trial" ? "正在试排…" : "试排结构";
  elements.formalTemplateApply.textContent = state.formalTemplateBusy === "select" ? "正在保存…" : "用于当前项目";
}

function renderFormalTemplatePreview() {
  renderTemplateToneOptions();
  const item = state.formalTemplates?.items?.find((entry) => entry.ref === state.formalTemplateRef);
  elements.formalTemplatePreview.hidden = !item;
  if (!item) { elements.formalTemplatePreview.innerHTML = ""; return; }
  const structures = (item.structures || []).map((part) => `<li><strong>${escapeHtml(part.name)}</strong><span>第 ${escapeHtml(part.start)}–${escapeHtml(part.end)} 镜 · ${escapeHtml(part.shot_count)} 镜</span></li>`).join("");
  const warnings = (item.warnings || []).map((warning) => `<li>${escapeHtml(typeof warning === "string" ? warning : warning.message || "请核对模板内容")}</li>`).join("");
  const semantics = item.source_semantics;
  const original = semantics?.status === 'recorded' ? `<details><summary>原库整体说明与当前结构</summary><p>${escapeHtml(semantics.structure_policy)}</p>${(semantics.sheets || []).map(sheet => `<section><h4>${escapeHtml(sheet.source_sheet)}</h4>${(sheet.overview_cells || []).map(cell => `<p style="white-space:pre-wrap">${escapeHtml(cell.text)}</p><small>${escapeHtml(item.source_name)} · ${escapeHtml(sheet.source_sheet)} · ${escapeHtml(cell.cell)}</small>`).join('')}</section>`).join('')}</details>` : '<p>原库整体说明：此版本未记录。</p>';
  elements.formalTemplatePreview.innerHTML = `<div class="panel-kicker"><span>核对结构</span><b>${escapeHtml(item.shot_count)} 镜</b></div><h3>${escapeHtml(formalTemplateLabel(item))}</h3><ol class="template-structures">${structures || "<li>暂无结构摘要</li>"}</ol>${original}${formalTemplateSourcesHtml(item.sources)}${warnings ? `<details class="template-warnings" open><summary>待核对</summary><ul>${warnings}</ul></details>` : ""}<details><summary>来源</summary><p>${escapeHtml(item.source_name || "未提供")}</p></details>`;
}

function renderTemplateToneOptions() {
  const host = document.querySelector('#formal-template-tones');
  const item = state.formalTemplates?.items?.find(entry => entry.ref === state.formalTemplateRef);
  if (!host || !item) { if (host) host.replaceChildren(); return; }
  const options = item.tone_options || [];
  const input = formalBriefInput(formalRoute().projectId);
  const draft = input.toneDraft || '';
  host.innerHTML = `<h3>选择表达调性</h3><p>来自「${escapeHtml(formalTemplateLabel(item))}」；选择后仍可调整。</p><div class="brief-tone-options">${options.map((option, index) => `<button type="button" class="text-button" data-template-tone="${index}" aria-pressed="${draft === option.value}"><strong>${escapeHtml(option.label || option.value)}</strong><small>${escapeHtml(option.source_label || '模板建议')}</small></button>`).join('')}</div>${!options.length ? '<p>此模板尚无调性记录，可以填写你想要的表达。</p>' : ''}<form id="brief-tone-form"><label><span>本次调性</span><input id="brief-tone-value" type="text" maxlength="2000" value="${escapeHtml(draft)}" placeholder="选择上方建议，或填写自己的调性" required></label><button type="submit" class="secondary-button compact">保存调性</button><p role="status" id="brief-tone-status"></p></form>`;
}

document.querySelector('#formal-template-tones').addEventListener('click', event => {
  const button = event.target.closest('[data-template-tone]'); if (!button) return;
  const item = state.formalTemplates?.items?.find(entry => entry.ref === state.formalTemplateRef);
  const option = item?.tone_options?.[Number(button.dataset.templateTone)]; if (!option) return;
  const input = formalBriefInput(formalRoute().projectId); input.toneDraft = option.value;
  persistFormalBriefInput(formalRoute().projectId, input); renderTemplateToneOptions();
});
document.querySelector('#formal-template-tones').addEventListener('input', event => {
  if (!event.target.matches('#brief-tone-value')) return;
  const input = formalBriefInput(formalRoute().projectId); input.toneDraft = event.target.value;
  persistFormalBriefInput(formalRoute().projectId, input);
});
document.querySelector('#formal-template-tones').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target.closest('#brief-tone-form'); if (!form || !form.reportValidity()) return;
  const projectId = formalRoute().projectId, input = formalBriefInput(projectId), value = form.querySelector('input').value.trim();
  const workspaceId = state.agentWorkspaceId, briefVersionId = state.formalBrief?.brief_key;
  if (input.busy || !value) return;
  input.busy = true; form.querySelector('button').disabled = true;
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    await api(`/api/formal/projects/${encodeURIComponent(projectId)}/brief/field?workspace_id=${encodeURIComponent(workspaceId)}`, {method: 'POST', body: JSON.stringify({brief_version_id: briefVersionId, field_label: '风格调性', value, expected_project_version: project.project_version})});
    input.toneDraft = value;
    if (state.agentWorkspaceId === workspaceId) persistFormalBriefInput(projectId, input);
    if (!isFormalBriefProject(projectId) || state.agentWorkspaceId !== workspaceId) return;
    await loadFormalBrief(projectId);
    if (isFormalBriefProject(projectId)) document.querySelector('#brief-tone-status').textContent = '调性已保存，请核对需求后确认。';
  } catch (error) { if (form.isConnected) form.querySelector('[role=status]').textContent = `${formalError(error)} 输入已保留。`; }
  finally { input.busy = false; if (form.isConnected) form.querySelector('button').disabled = false; }
});

function formalTemplateSourcesHtml(sources) {
  if (!sources) return '<p>此记录未提供关联素材概览。</p>';
  return `<section class="template-sources"><h4>将使用的创意库与图库</h4><p>模板类别：${escapeHtml(sources.category_label || sources.category_id || '')} · 创意 ${Number(sources.creative_count || 0)} 条 · 图片 ${Number(sources.image_count || 0)} 张（按内容去重）</p><ul>${(sources.libraries || []).map(library => `<li><strong>${escapeHtml(library.label || library.ref)}</strong> · ${escapeHtml(library.category_label || library.category_id || '')} · 版本 ${escapeHtml(library.version)} · ${library.role === 'template' ? '模板自带素材' : '已生效共享库'} · 创意 ${Number(library.creative_count || 0)} 条 · 图片 ${Number(library.image_count || 0)} 张</li>`).join('')}</ul>${(sources.warnings || []).map(warning => `<p role="status">${escapeHtml(typeof warning === 'string' ? warning : warning.message || '')}</p>`).join('')}</section>`;
}

function renderFormalTemplateCatalog(catalog, previewRef = "") {
  state.formalTemplates = catalog;
  const selected = formalTemplateSelected(catalog);
  const categorySelect = document.querySelector('#formal-template-category');
  categorySelect.innerHTML = (catalog.categories || []).map(item => `<option value="${escapeHtml(item.category_id)}">${escapeHtml(item.label)}</option>`).join('');
  categorySelect.value = catalog.category_id || '';
  state.formalTemplateRef = catalog.items?.some((item) => item.ref === previewRef) ? previewRef : selected?.ref || "";
  invalidateFormalTemplateInput();
  const currentLabel = !catalog.category_id && !catalog.items?.length ? "确认项目类型后选择模板" : !selected && catalog.is_bound ? "已绑定模板不可用，请重新选择" : formalTemplateLabel(selected);
  elements.formalTemplateCurrent.textContent = currentLabel;
  elements.formalPlanTemplate.textContent = currentLabel;
  elements.formalTemplateSelect.innerHTML = (catalog.items || []).map((item) => `<option value="${escapeHtml(item.ref)}">${escapeHtml(formalTemplateLabel(item))}${item.selected ? " · 当前使用" : ""}</option>`).join("");
  elements.formalTemplateSelect.value = state.formalTemplateRef;
  elements.formalTemplateNotice.textContent = catalog.notice || "选择后核对结构，再用于当前项目。";
  elements.formalTemplateRetry.hidden = true;
  renderFormalTemplatePreview();
  renderFormalTemplateControls();
  renderTemplateToneOptions();
}

async function loadFormalTemplates(projectId, page = "brief", targetCategoryId = '') {
  if (!projectId || !["brief", "plan"].includes(page) || !state.agentAuth || !state.agentWorkspaceId) return;
  if (formalRoute().projectId !== projectId || formalSurface() !== page) return;
  syncFormalTemplateRoute();
  const epoch = ++state.formalTemplateEpoch;
  state.formalTemplates = null;
  state.formalTemplateBusy = "load";
  elements.formalTemplatePreview.innerHTML = "";
  elements.formalTemplatePreview.hidden = true;
  invalidateFormalTemplateInput();
  renderFormalTemplateControls();
  elements.formalTemplateNotice.textContent = "正在读取可选模板…";
  elements.formalPlanTemplateLink.href = formalPath("brief", projectId, { panel: "templates" });
  try {
    const catalog = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/templates?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}${targetCategoryId ? '&target_category_id=' + encodeURIComponent(targetCategoryId) : ''}`);
    if (!formalTemplateIsCurrent(projectId, page, epoch)) return;
    renderFormalTemplateCatalog(catalog);
    if (page === "brief" && new URLSearchParams(window.location.search).get("panel") === "templates") {
      elements.formalTemplatePicker.open = true;
      elements.formalTemplatePicker.scrollIntoView({ block: "start" });
    }
  } catch (error) {
    if (!formalTemplateIsCurrent(projectId, page, epoch)) return;
    elements.formalTemplateCurrent.textContent = "模板读取失败";
    elements.formalPlanTemplate.textContent = "模板读取失败，请返回需求页重试";
    elements.formalTemplateNotice.textContent = formalError(error);
    elements.formalTemplateRetry.hidden = false;
  } finally {
    if (formalTemplateIsCurrent(projectId, page, epoch)) {
      state.formalTemplateBusy = "";
      renderFormalTemplateControls();
    }
  }
}

function changeFormalTemplate() {
  state.formalTemplateRef = elements.formalTemplateSelect.value;
  elements.formalTemplateNotice.textContent = "请核对所选模板的结构。";
  invalidateFormalTemplateInput();
  renderFormalTemplatePreview();
  renderFormalTemplateControls();
}

async function runFormalTemplateAction(action) {
  const catalog = state.formalTemplates;
  if (!formalTemplateWritable() || state.formalTemplateBusy || !catalog) return;
  const item = catalog.items?.find((entry) => entry.ref === state.formalTemplateRef);
  const file = elements.formalTemplateFile.files[0];
  if (action === "import" ? !file : !item) return;
  if (action === "trial" && !catalog.can_trial) return;
  if (action === "select" && (!elements.formalTemplateConfirm.checked || (catalog.is_bound !== false && item.ref === formalTemplateSelected(catalog)?.ref))) return;
  const projectId = catalog.project_id;
  const epoch = state.formalTemplateEpoch;
  const revision = state.formalTemplateInputRevision;
  const active = () => formalTemplateIsCurrent(projectId, "brief", epoch) && revision === state.formalTemplateInputRevision && state.formalTemplates === catalog;
  state.formalTemplateBusy = action;
  renderFormalTemplateControls();
  elements.formalTemplateNotice.textContent = { import: "正在读取并核对结构…", trial: "正在按当前 Brief 试排…", select: "正在保存项目模板…" }[action];
  try {
    let body;
    if (action === "import") {
      body = new FormData();
      body.append("file", file);
      body.append("workspace_id", state.agentWorkspaceId);
      body.append("expected_project_version", String(catalog.project_version));
    } else {
      body = JSON.stringify({ workspace_id: state.agentWorkspaceId, expected_project_version: catalog.project_version, template_ref: item.ref,
        ...(action === "select" ? { confirmed: true } : { generation_mode: "text_only_draft" }) });
    }
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/templates/${action}`, { method: "POST", body });
    if (!active()) return;
    if (action === "trial") {
      state.formalTemplateTrial = result;
      const plan = result.plan || {};
      const rows = (plan.shots || []).map((shot, index) => `<tr><td>${escapeHtml(shot.shot_no || index + 1)}</td><td>${escapeHtml(shot.structure || "—")}</td><td>${escapeHtml(shot.primary_event || "—")}</td><td>${escapeHtml(shot.duration_seconds || "—")} 秒</td></tr>`).join("");
      elements.formalTemplateTrialResult.innerHTML = `<h3>试排结果 · ${escapeHtml(plan.shot_count || plan.shots?.length || 0)} 镜</h3><p>仅供核对，正式生成请前往「规划与生成」。</p><div class="template-trial-table"><table><thead><tr><th>镜号</th><th>结构</th><th>内容</th><th>时长</th></tr></thead><tbody>${rows}</tbody></table></div>`;
      elements.formalTemplateTrialResult.hidden = false;
      elements.formalTemplateNotice.textContent = planHasDecisionGate(plan) ? "试排完成，仍有待核对项；请在规划页处理。" : "试排完成，请核对下方结构与内容。";
    } else {
      renderFormalTemplateCatalog(result, action === "import" ? result.preview?.ref : "");
      if (action === "import") {
        elements.formalTemplateFile.value = "";
        elements.formalTemplateNotice.textContent = "导入完成，请核对结构；尚未用于项目。";
      } else {
        state.formalPlan = null;
        state.pendingPlanConflictAction = null;
        state.uiInputBaselines.delete(elements.formalTemplateSelect);
        elements.formalTemplateNotice.textContent = result.notice || "项目模板已更新，可继续规划与生成。";
        const project = state.formalProjects.find((entry) => entry.project_id === projectId);
        if (project) project.project_version = result.project_version;
      }
    }
  } catch (error) {
    if (!active()) return;
    elements.formalTemplateNotice.textContent = formalError(error);
    if (error?.payload?.detail?.code === "PROJECT_VERSION_CONFLICT") {
      const refresh = loadFormalTemplates(projectId, "brief");
      const refreshEpoch = state.formalTemplateEpoch;
      await refresh;
      if (formalTemplateIsCurrent(projectId, "brief", refreshEpoch)) elements.formalTemplateNotice.textContent = "项目已更新，请重新核对模板后再操作。";
    }
  } finally {
    if (formalTemplateIsCurrent(projectId, "brief", epoch)) {
      state.formalTemplateBusy = "";
      renderFormalTemplateControls();
    }
  }
}

async function importFormalTemplate() { return runFormalTemplateAction("import"); }
async function trialFormalTemplate() { return runFormalTemplateAction("trial"); }
async function selectFormalTemplate() { return runFormalTemplateAction("select"); }

function formalBriefId(projectId) {
  return isFormalBriefProject(projectId) ? state.formalPendingBriefKey : "";
}

function isFormalBriefProject(projectId) {
  return Boolean(projectId && state.formalBriefProjectId === projectId
    && formalRoute().projectId === projectId && formalRoute().page === "brief");
}

const briefDetailFields = ["脚本类型", "品牌或客户", "目标时长", "视频目标", "使用场景", "风格调性", "必须出现的内容", "关键事件与流程", "禁止或不能出现的内容", "特殊需求"];
document.querySelector("#brief-detail-fields").insertAdjacentHTML("beforeend", briefDetailFields.map(field =>
  `<label><span>${escapeHtml(field)}</span><textarea rows="2" maxlength="2000" data-brief-detail-field="${escapeHtml(field)}" placeholder="填写已确定的内容"></textarea></label>`).join(""));

function briefIntakeText() {
  const input = formalBriefInput(state.formalBriefProjectId);
  const rows = input.mode === "details" ? [...document.querySelectorAll("[data-brief-detail-field]")]
    .filter(field => field.value.trim()).map(field => `${field.dataset.briefDetailField}：${field.value.trim().replace(/\n/g, "；")}`) : [];
  return [...rows, elements.formalBriefText.value.trim()].filter(Boolean).join("\n");
}

function renderBriefMode(input) {
  const detailed = input?.mode === "details";
  document.querySelector("#brief-detail-fields").hidden = !detailed;
  document.querySelector("#brief-composer-label").textContent = detailed ? "其他要求或补充说明" : "你的需求或补充";
  document.querySelectorAll("[data-brief-mode]").forEach(button => button.setAttribute("aria-pressed", String((button.dataset.briefMode === "details") === detailed)));
}

document.querySelectorAll("[data-brief-mode]").forEach(button => button.addEventListener("click", () => {
  const input = formalBriefInput(state.formalBriefProjectId);
  rememberFormalBriefInput(); input.mode = button.dataset.briefMode;
  persistFormalBriefInput(state.formalBriefProjectId, input); renderBriefMode(input); renderFormalBriefSelection();
}));

document.querySelector("#brief-source-panel").addEventListener("toggle", event => {
  if (!event.target.open) return;
  const feed = document.querySelector("#brief-conversation");
  if (!feed.scrollTop) feed.scrollTop = feed.scrollHeight;
});

function rememberBriefSourcePanelInteraction() {
  if (isFormalBriefProject(state.formalBriefProjectId))
    formalBriefInput(state.formalBriefProjectId).sourcePanelInteracted = true;
}

for (const eventName of ['click', 'input', 'change', 'drop'])
  document.querySelector('#brief-source-panel').addEventListener(eventName, rememberBriefSourcePanelInteraction);

function briefQuestionBindings(input) {
  const bindings = new Map(Object.entries(input.questionBindings || {}));
  for (const item of input.conversation || []) for (const binding of item.response?.question_bindings || []) bindings.set(binding.question_id, binding);
  return bindings;
}

function renderBriefComposerContext(input) {
  const bindings = briefQuestionBindings(input), reply = bindings.get(input.replyQuestionId);
  const context = document.querySelector('#brief-reply-context');
  context.hidden = !reply;
  context.innerHTML = reply ? `<span>回复：${escapeHtml(reply.question)}</span><button type="button" class="text-button" data-brief-reply-clear>取消引用</button>` : '';
  const pending = Object.entries(input.questionAnswers || {}).filter(([id, value]) => value?.trim() && id !== input.replyQuestionId);
  const host = document.querySelector('#brief-answer-drafts');
  host.hidden = !pending.length;
  host.innerHTML = pending.length ? `<strong>另有 ${pending.length} 条回答草稿，将一起发送</strong>${pending.map(([id, value]) => `<div><span>${escapeHtml(bindings.get(id)?.question || '较早的问题')}：${escapeHtml(value)}</span><button type="button" class="text-button" data-brief-reply="${escapeHtml(id)}">编辑这条回答</button></div>`).join('')}` : '';
}

function renderBriefConversation(projectId) {
  if (!isFormalBriefProject(projectId)) return;
  const input = formalBriefInput(projectId), entries = input.conversation || [];
  const feed = document.querySelector('#brief-conversation');
  const nearEnd = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  const key = JSON.stringify([projectId, entries]);
  if (feed.dataset.conversationKey !== key) {
    const previousScroll = feed.scrollTop;
    feed.innerHTML = entries.length ? entries.map(item => {
      const reply = item.response;
      const stale = reply.brief_key && state.formalBrief?.brief_key && reply.brief_key !== state.formalBrief.brief_key;
      return `<article class="brief-chat-turn"><p class="brief-chat-user"><small>你</small>${escapeHtml(reply.user_message || '分析已存需求')}</p><div class="brief-chat-assistant"><strong>需求助手${stale ? ' · 较早分析' : ''}</strong><p>${escapeHtml(reply.summary || '')}</p>${reply.questions?.length ? `<div class="brief-chat-questions" aria-label="可回复的问题">${reply.questions.map(question => {
        const binding = reply.question_bindings?.find(item => item.question === question);
        return binding ? `<button type="button" class="brief-question" data-brief-reply="${escapeHtml(binding.question_id)}"><span>${escapeHtml(question)}</span><small>引用回复</small></button>` : `<p>${escapeHtml(question)}</p>`;
      }).join('')}</div>` : ''}${reply.error_code ? '<small>资料已保留，可重新分析。</small>' : ''}</div></article>`;
    }).join('') : '<p class="brief-assistant-intro">告诉我这次想做什么，或上传已有资料。你可以一次回答多个问题，我会整理成可核对的需求。</p>';
    feed.dataset.conversationKey = key;
    feed.scrollTop = nearEnd || !previousScroll ? feed.scrollHeight : previousScroll;
  }
  input.questionBindings = Object.fromEntries(briefQuestionBindings(input));
  persistFormalBriefInput(projectId, input);
  renderBriefComposerContext(input);
}

document.querySelector('#brief-source-panel').addEventListener('click', event => {
  const reply = event.target.closest('[data-brief-reply]'), clear = event.target.closest('[data-brief-reply-clear]');
  if (!reply && !clear) return;
  const input = formalBriefInput(state.formalBriefProjectId);
  rememberFormalBriefInput();
  if (input.replyQuestionId && input.text.trim()) input.questionAnswers[input.replyQuestionId] = input.text;
  else if (!input.replyQuestionId) input.generalDraft = input.text;
  input.replyQuestionId = reply?.dataset.briefReply || '';
  input.text = input.replyQuestionId ? input.questionAnswers[input.replyQuestionId] || '' : input.generalDraft || '';
  input.mode = 'conversation'; input.editingField = '';
  persistFormalBriefInput(state.formalBriefProjectId, input);
  restoreFormalBriefInput(input); renderBriefComposerContext(input);
  elements.formalBriefText.focus({preventScroll: true});
});

async function submitBriefAnswers(projectId, input, {analyze = true} = {}) {
  input.lastAnswersSaved = false;
  if (!input.sessionId) await loadBriefConversation(projectId);
  const bindings = briefQuestionBindings(input);
  let answers = Object.entries(input.questionAnswers || {}).filter(([, content]) => content.trim()).map(([question_id, content]) => ({question_id, analysis_task_id: bindings.get(question_id)?.analysis_task_id || question_id.split(':').slice(0, -1).join(':'), content}));
  if (!answers.length && !input.answerRequest) return false;
  let content = input.replyQuestionId ? input.generalDraft || '' : input.text;
  const fingerprint = JSON.stringify([answers, content]);
  if (input.answerRequest) { answers = input.answerRequest.body.answers; content = input.answerRequest.body.content || ''; }
  input.busy = true; restoreFormalBriefInput(input);
  document.querySelector('#brief-intake-status').textContent = `正在保存 ${answers.length} 条回答…`;
  let saved = false;
  try {
    if (!input.answerRequest) {
      const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
      input.answerRequest = {fingerprint, body: {answers, content, expected_project_version: project.project_version, idempotency_key: formalRequestKey('brief-answers')}};
      persistFormalBriefInput(projectId, input);
    }
    const result = await api(`/api/agent/sessions/${encodeURIComponent(input.sessionId)}/brief-answers`, {method: 'POST', body: JSON.stringify(input.answerRequest.body)});
    saved = true; input.lastAnswersSaved = true;
    const accepted = new Set(result.answered_question_ids || answers.map(item => item.question_id));
    for (const answer of answers) if (accepted.has(answer.question_id) && input.questionAnswers[answer.question_id] === answer.content) delete input.questionAnswers[answer.question_id];
    if (input.replyQuestionId && accepted.has(input.replyQuestionId) && !input.questionAnswers[input.replyQuestionId]) { input.replyQuestionId = ''; input.text = ''; }
    else if (!input.replyQuestionId && input.text === content) input.text = '';
    if (input.generalDraft === content) input.generalDraft = '';
    input.answerRequest = null;
    persistFormalBriefInput(projectId, input);
    if (!isFormalBriefProject(projectId)) return true;
    restoreFormalBriefInput(input);
    await loadFormalBrief(projectId);
    if (analyze) await analyzeStoredBrief(projectId, [...answers.map(item => `${bindings.get(item.question_id)?.question || '回答'}：${item.content}`), content].filter(Boolean).join('\n'));
  } catch (error) {
    if (!saved && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) input.answerRequest = null;
    persistFormalBriefInput(projectId, input);
    if (isFormalBriefProject(projectId)) {
      document.querySelector('#brief-intake-status').textContent = `${saved ? '回答已保存，分析尚未完成。' : '回答草稿已保留，请重试发送。'}${formalError(error)}`;
      document.querySelector('#brief-analysis-retry').hidden = !saved;
    }
  } finally {
    input.busy = false;
    if (isFormalBriefProject(projectId)) { restoreFormalBriefInput(input); renderBriefComposerContext(input); }
  }
  return true;
}

async function loadBriefConversation(projectId) {
  const input = formalBriefInput(projectId);
  try {
    const session = await ensureAgentProjectSession(projectId);
    input.sessionId = session.session.agent_session_id;
    input.conversation = (session.timeline || []).filter(item => item.response?.type === "brief_analysis");
    input.conversationError = ""; renderBriefConversation(projectId);
  } catch (error) {
    input.conversationError = formalError(error);
    if (isFormalBriefProject(projectId)) document.querySelector("#brief-intake-status").textContent = `分析记录暂未读到：${input.conversationError}`;
  }
}

async function analyzeStoredBrief(projectId, message = "请分析当前已录入的需求，集中说明还需要补充什么。") {
  const input = formalBriefInput(projectId);
  if (!input.sessionId) await loadBriefConversation(projectId);
  if (!input.sessionId) throw new Error(input.conversationError || "需求分析会话尚未连接");
  if (isFormalBriefProject(projectId)) document.querySelector("#brief-intake-status").textContent = "资料已保存，助手正在分析需求…";
  const result = await api(`/api/agent/sessions/${encodeURIComponent(input.sessionId)}/brief-analysis`, {
    method: "POST", body: JSON.stringify({content: message.slice(0, 4000)}),
  });
  await loadBriefConversation(projectId);
  if (isFormalBriefProject(projectId)) {
    document.querySelector("#brief-intake-status").textContent = result.response?.error_code ? result.response.summary : "分析已保存，请核对需求摘要；待补信息可以一次回复。";
    document.querySelector("#brief-analysis-retry").hidden = !(state.formalBrief?.brief?.documents || []).length;
    // Conversation rendering preserves the reading position.
  }
}

document.querySelector("#brief-analysis-retry").addEventListener("click", async () => {
  const projectId = state.formalBriefProjectId, input = formalBriefInput(projectId);
  if (input.busy) return;
  input.busy = true; restoreFormalBriefInput(input);
  try { await analyzeStoredBrief(projectId); }
  catch (error) { if (isFormalBriefProject(projectId)) document.querySelector("#brief-intake-status").textContent = formalError(error); }
  finally { input.busy = false; if (isFormalBriefProject(projectId)) restoreFormalBriefInput(input); }
});

document.querySelector("#brief-next-step").addEventListener("click", event => {
  const button = event.target.closest("[data-brief-fill-missing]");
  if (!button) return;
  rememberFormalBriefInput();
  const input = formalBriefInput(state.formalBriefProjectId);
  input.mode = "details"; renderBriefMode(input);
  rememberBriefSourcePanelInteraction();
  document.querySelector("#brief-source-panel").open = true;
  const field = [...document.querySelectorAll("[data-brief-detail-field]")].find(item => item.dataset.briefDetailField === button.dataset.briefFillMissing.split(",")[0]);
  field?.scrollIntoView({block: "center", behavior: "smooth"}); field?.focus({preventScroll: true});
  persistFormalBriefInput(state.formalBriefProjectId, input);
});

function formalBriefDraftKey(projectId) {
  return `mplus.brief-inputs.${state.agentWorkspaceId}.${projectId}`;
}
function persistFormalBriefInput(projectId, input) {
  try { sessionStorage.setItem(formalBriefDraftKey(projectId), JSON.stringify({text: input.text, editingField: input.editingField, drafts: input.drafts, mode: input.mode, details: input.details, questionAnswers: input.questionAnswers, questionBindings: input.questionBindings, replyQuestionId: input.replyQuestionId, generalDraft: input.generalDraft, answerRequest: input.answerRequest, toneDraft: input.toneDraft})); }
  catch (_error) { /* Keep the live draft if storage is unavailable. */ }
}
function formalBriefInput(projectId) {
  if (!state.formalBriefInputs.has(projectId)) {
    let saved = {};
    try { saved = JSON.parse(sessionStorage.getItem(formalBriefDraftKey(projectId)) || "{}"); } catch (_error) {}
    state.formalBriefInputs.set(projectId, { text: saved.text || "", files: [], editingField: saved.editingField || "", drafts: saved.drafts || {}, mode: saved.mode || "conversation", details: saved.details || {}, questionAnswers: saved.questionAnswers || {}, questionBindings: saved.questionBindings || {}, replyQuestionId: saved.replyQuestionId || "", generalDraft: saved.generalDraft || "", answerRequest: saved.answerRequest || null, toneDraft: saved.toneDraft || "", sourcePanelInteracted: false, revision: 0, busy: false });
  }
  return state.formalBriefInputs.get(projectId);
}

function rememberFormalBriefInput() {
  if (!state.formalBriefProjectId) return;
  const input = formalBriefInput(state.formalBriefProjectId);
  input.text = elements.formalBriefText.value;
  if (input.replyQuestionId) input.questionAnswers[input.replyQuestionId] = input.text;
  input.files = [...elements.formalBriefFiles.files];
  input.editingField = elements.formalBriefUpload.dataset.editFactField || "";
  input.details = Object.fromEntries([...document.querySelectorAll("[data-brief-detail-field]")].map(field => [field.dataset.briefDetailField, field.value]));
  input.drafts[input.editingField] = {...input.drafts[input.editingField], text: input.text};
  persistFormalBriefInput(state.formalBriefProjectId, input);
}

function restoreFormalBriefInput(input) {
  elements.formalBriefText.value = input?.text || "";
  const transfer = new DataTransfer();
  (input?.files || []).forEach((file) => transfer.items.add(file));
  elements.formalBriefFiles.files = transfer.files;
  if (input?.editingField) elements.formalBriefUpload.dataset.editFactField = input.editingField;
  else delete elements.formalBriefUpload.dataset.editFactField;
  elements.formalBriefUploadButton.disabled = Boolean(input?.busy);
  document.querySelector("#brief-analysis-retry").disabled = Boolean(input?.busy);
  elements.formalBriefUploadButton.textContent = input?.busy ? "正在处理…" : input?.editingField ? "保存并重新分析" : "发送";
  document.querySelectorAll("[data-brief-detail-field]").forEach(field => { field.value = input?.details?.[field.dataset.briefDetailField] || ""; });
  renderBriefMode(input);
  renderFormalBriefSelection();
  renderBriefComposerContext(input || {});
}

function syncFormalBriefProject() {
  const route = formalRoute();
  const projectId = route.page === "brief" ? route.projectId || "" : "";
  if (state.formalBriefProjectId === projectId) return;
  rememberFormalBriefInput();
  state.formalBriefProjectId = projectId;
  state.formalBriefEpoch += 1;
  state.formalBrief = null;
  state.formalBriefUploadLimits = null;
  state.formalPendingBriefKey = "";
  const input = projectId ? formalBriefInput(projectId) : null;
  if (input) input.sourcePanelInteracted = false;
  restoreFormalBriefInput(input);
  emptyFormalBrief();
  elements.formalBriefConfirm.textContent = "确认需求";
}

function emptyFormalBrief() {
  if (!state.formalBriefProjectId || !formalBriefInput(state.formalBriefProjectId).sourcePanelInteracted)
    document.querySelector("#brief-source-panel").open = true;
  elements.formalBriefWorkbench.classList.remove("is-confirmed");
  document.querySelector("#brief-enter-creative").hidden = true;
  elements.formalBriefConfirm.hidden = false;
  elements.formalBriefWorkbench.prepend(document.querySelector("#brief-next-step"));
  elements.formalBriefReadiness.textContent = "等待资料";
  elements.formalBriefFilesState.innerHTML = '<div class="workbench-empty">还没有读取项目资料。选择文件后，系统会显示资料状态。</div>';
  elements.formalFactList.innerHTML = '<div class="workbench-empty">暂无已识别事实。</div>';
  elements.formalConflictList.innerHTML = '<div class="workbench-empty">暂无待确认冲突。</div>';
  elements.formalFactCount.textContent = "0 项";
  elements.formalConflictCount.textContent = "0 项";
  elements.formalBriefGateTitle.textContent = "等待资料读取";
  elements.formalBriefGateCopy.textContent = "在下方描述需求或上传文档，助手分析后集中核对。";
  document.querySelector("#brief-template-panel").classList.remove("brief-template-pending");
  document.querySelector("#brief-intake-status").textContent = "";
  document.querySelector("#brief-analysis-retry").hidden = true;
  renderBriefConversation(state.formalBriefProjectId);
  elements.formalBriefRouteState.textContent = "等待资料";
  elements.formalBriefRouteCopy.textContent = "读取脚本类型后，会在确认前说明采用哪一类模板。";
  elements.formalBriefRouteDetails.innerHTML = "";
  elements.formalBriefConfirm.disabled = true;
  renderFormalStandardCard(null);
}

function renderFormalBriefRouting(routing) {
  const route = routing && typeof routing === "object" ? routing : {};
  const ready = route.status === "ready";
  elements.formalBriefRouteState.textContent = ready ? `已识别：${route.script_type}` : "类型待识别";
  const categoryControl = document.querySelector("#formal-brief-category-choice");
  if (categoryControl) categoryControl.hidden = ready;
  elements.formalBriefRouteCopy.textContent = route.message || "当前资料还不能确定生成模板。";
  const rows = [
    ["脚本类型原文", route.raw_script_type || "未提供"],
    ["具体类型原文", route.specific_script_type || "未提供"],
    ["识别类别", ready ? route.script_type : "尚未确定"],
    ["类别默认来源", ready ? route.source_workbook_name : "尚未确定"],
  ];
  elements.formalBriefRouteDetails.innerHTML = rows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
  return { ready, blocked: !ready };
}

async function loadFormalBrief(projectId) {
  if (!isFormalBriefProject(projectId)) return;
  const epoch = ++state.formalBriefEpoch;
  const available = Boolean(projectId && state.formalBriefAvailable && state.agentAuth && state.agentWorkspaceId);
  elements.formalBriefProjectRequired.hidden = available;
  elements.formalBriefWorkbench.hidden = !available;
  if (!available) { elements.formalBriefReadiness.textContent = "等待项目"; return; }
  try {
    const path = `/api/formal/projects/${encodeURIComponent(projectId)}/brief/current?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
    await Promise.all([
      api(path).then((current) => {
        if (!isFormalBriefProject(projectId) || epoch !== state.formalBriefEpoch) return;
        state.formalBriefUploadLimits = current.upload_limits || null;
        renderFormalBriefSelection();
        if (current.status === "empty") {
          state.formalPendingBriefKey = "";
          emptyFormalBrief();
          return;
        }
        state.formalPendingBriefKey = current.confirmation_status === "confirmed" ? "" : current.brief_key;
        renderFormalBrief(current);
      }),
      loadFormalTemplates(projectId, "brief"),
      loadBriefConversation(projectId),
    ]);
  } catch (error) {
    if (!isFormalBriefProject(projectId) || epoch !== state.formalBriefEpoch) return;
    elements.formalBriefReadiness.textContent = "暂不可用";
    elements.formalBriefFilesState.innerHTML = `<div class="workbench-empty">${escapeHtml(formalError(error))}</div>`;
  }
}

function formalValue(value) {
  try { const parsed = JSON.parse(value); return typeof parsed === "string" ? parsed : Array.isArray(parsed) ? parsed.join("、") : String(value || ""); } catch { return String(value || ""); }
}

function standardFieldLabel(field) {
  return ({ tone_keywords: "调性", expression_focus: "重点", avoid_expressions: "禁用表达", references: "参考资料" })[field] || field;
}

function renderFormalStandardCard(standard) {
  if (!standard || typeof standard !== "object") {
    elements.formalStandardCardState.textContent = "等待资料";
    elements.formalStandardCardCopy.textContent = "读取资料后会在这里显示项目创作标准。";
    elements.formalStandardCardDetails.innerHTML = "";
    return { unavailable: false, confirmed: false, missing: ["tone_keywords", "expression_focus", "avoid_expressions"] };
  }
  const unavailable = standard?.status === "unavailable";
  const confirmed = standard?.status === "confirmed";
  const missing = Array.isArray(standard?.missing_fields) ? standard.missing_fields : [];
  const fields = ["tone_keywords", "expression_focus", "avoid_expressions", "references"];
  elements.formalStandardCardState.textContent = confirmed ? "已确认" : unavailable ? "需重新导入" : "待确认";
  if (confirmed) {
    elements.formalStandardCardCopy.textContent = `已与当前 Brief 一同确认${standard.confirmed_at ? ` · ${formatDate(Date.parse(standard.confirmed_at) / 1000)}` : ""}。`;
    elements.formalStandardCardDetails.innerHTML = "";
    return { unavailable: false, confirmed: true, missing: [] };
  }
  elements.formalStandardCardCopy.textContent = unavailable
    ? "需重新导入资料生成标准卡。"
    : missing.length
      ? `请补充${missing.map(standardFieldLabel).join("、")}后再确认。`
      : "请核对四项创作标准后，与 Brief 一同确认。";
  elements.formalStandardCardDetails.innerHTML = fields.map((field) => {
    const entries = Array.isArray(standard?.[field]) ? standard[field] : [];
    const empty = field === "references" ? "客户未提供参考资料" : field === "avoid_expressions" ? "客户未提供（不阻断确认）" : "尚未识别到这项标准";
    const content = entries.length ? entries.map((entry) => {
      const sources = Array.isArray(entry.sources) ? entry.sources : [];
      const source = sources.length ? `<details><summary>查看来源</summary><ul>${sources.map((label) => `<li>${escapeHtml(label)}</li>`).join("")}</ul></details>` : "";
      return `<li><strong>${escapeHtml(entry.value || "未提供")}</strong>${source}</li>`;
    }).join("") : `<li class="ui00-standard-empty">${empty}</li>`;
    const blocked = missing.includes(field);
    return `<section class="ui00-standard-field${blocked ? " is-missing" : ""}"><header><span>${standardFieldLabel(field)}</span>${blocked ? "<b>需要补充</b>" : ""}</header><ul>${content}</ul>${field === 'tone_keywords' ? '<button type="button" class="secondary-button compact" data-brief-tone-open>从模板选择调性</button>' : ''}</section>`;
  }).join("");
  return { unavailable, confirmed, missing };
}

document.querySelector('#formal-standard-card-details').addEventListener('click', event => {
  if (!event.target.closest('[data-brief-tone-open]')) return;
  elements.formalTemplatePicker.open = true;
  document.querySelector('#formal-template-tones').scrollIntoView({block: 'center'});
  document.querySelector('#brief-tone-value')?.focus({preventScroll: true});
});

function renderFormalBrief(response) {
  const brief = response?.brief || {};
  const confirmationStatus = response?.confirmation_status || "pending";
  const projectId = formalRoute().projectId;
  state.formalBrief = response;
  state.formalBriefUploadLimits = response?.upload_limits || state.formalBriefUploadLimits;
  renderFormalBriefSelection();
  const documents = brief.documents || [];
  document.querySelector("#brief-analysis-retry").hidden = !documents.length;
  const allFacts = brief.facts || [];
  const facts = allFacts.filter(fact => fact.adopted !== false);
  const conflicts = brief.conflicts || [];
  const warnings = brief.warnings || [];
  elements.formalBriefFilesState.innerHTML = documents.length ? documents.map((document) => {
    const status = document.status === "failed" ? "读取失败" : document.status === "succeeded" ? "已读取" : "处理中";
    const detail = document.status === "failed"
      ? (document.error?.message || "此文件未能完成读取。请替换文件或处理后重试。")
      : (document.locations || []).join("；") || "来源位置暂未提供。";
    const failed = document.status === "failed" || (document.ocr || []).some(item => ["failed", "unavailable"].includes(item.status));
    const recovery = failed ? `<div class="brief-inline-edit-actions"><button type="button" class="secondary-button compact" data-brief-file-retry="${escapeHtml(document.document_key || "")}">重试读取此文件</button><button type="button" class="text-button" data-brief-file-replace="${escapeHtml(document.document_key || "")}">替换此文件</button></div><p data-brief-retry-state role="status">识别未完成，文件已保留；可重试。需要修正文件内容时选择替换。</p>` : "";
    return `<details class="ui00-source-item" data-state="${escapeHtml(document.status || "pending")}"><summary><span>${escapeHtml(document.filename || "未命名文件")}</span><b>${status}</b></summary><p>${escapeHtml(detail)}</p>${recovery}</details>`;
  }).join("") : '<div class="workbench-empty">暂无来源文件。</div>';
  const history = allFacts.filter(fact => fact.adopted === false);
  if (history.length) elements.formalBriefFilesState.insertAdjacentHTML("beforeend", `<details class="brief-fact-history"><summary>未采用的历史内容 · ${history.length} 项</summary>${history.map(fact => `<article class="ui00-fact-item"><small>${escapeHtml(fact.label)}</small><p>${escapeHtml(formalValue(fact.value))}</p><small>${escapeHtml((fact.sources || []).join("；"))}</small></article>`).join("")}</details>`);
  elements.formalFactCount.textContent = `${facts.length} 项`;
  const warningHtml = warnings.map((warning) => `<article class="ui00-fact-item"><small>资料提示</small><strong>${escapeHtml(warning.message || "资料存在需要留意的差异")}</strong></article>`).join("");
  elements.formalFactList.innerHTML = warningHtml + (facts.length ? facts.map((fact) => {
    const sources = fact.sources || [];
    const semantics = fact.semantic_metadata || {};
    const kind = ({project_fact:'事实', content_goal:'内容目标', mandatory_content:'必须内容', forbidden:'禁止项', event_sequence:'事件流程', production_requirement:'制作要求', style_preference:'风格要求', delivery_requirement:'交付要求', creative_suggestion:'创作建议'})[semantics.classification] || '分类未记录';
    const origin = ({literal_extraction:'原文直接提取', manual_field_edit:'人工填写', model_extraction:'智能整理', mixed_extraction:'原文与智能整理'})[semantics.origin] || '提取方式未记录';
    const quotes = (semantics.quotes || []).map(quote => `<blockquote>${escapeHtml(quote)}</blockquote>`).join('');
    const status = fact.requires_review ? `待核对，暂不用于创作。${semantics.support_note || ''} 可修改此字段后采用。` : fact.adopted === false ? '未采用' : '当前采用';
    const sourceDetail = `<details><summary>查看来源与整理方式</summary><p>${escapeHtml(kind)} · ${semantics.scope === 'whole_video' ? '全片' : semantics.scope === 'project' ? '项目' : '作用范围未记录'} · ${escapeHtml(origin)}</p><p>${escapeHtml(status)}</p>${quotes}<ul>${sources.map((source) => `<li>${escapeHtml(source)}</li>`).join('')}</ul></details>`;
    return `<article class="ui00-fact-item"><small>${escapeHtml(fact.label || "资料事实")}</small><strong>${escapeHtml(formalValue(fact.value))}</strong>${sourceDetail}<button class="secondary-button compact" type="button" data-edit-formal-fact="${escapeHtml(fact.label || "")}" data-edit-formal-value="${escapeHtml(formalValue(fact.value))}">修改此字段</button></article>`;
  }).join("") : '<div class="workbench-empty">暂无已识别事实。</div>');
  renderFormalConflicts(conflicts, allFacts);
  const failed = documents.some((item) => item.status === "failed" && item.critical !== false);
  const unresolved = conflicts.some((item) => item.resolution_status !== "resolved");
  const standard = renderFormalStandardCard(response?.creative_standard);
  const routing = renderFormalBriefRouting(response?.routing);
  const confirmed = !state.formalPendingBriefKey && confirmationStatus === "confirmed" && standard.confirmed;
  const standardsBlocked = standard.unavailable || standard.missing.length > 0;
  elements.formalBriefWorkbench.classList.toggle("is-confirmed", confirmed);
  document.querySelector("#page-brief").classList.toggle("brief-confirmed", confirmed);
  const sourcePanel = document.querySelector("#brief-source-panel");
  const input = formalBriefInput(projectId);
  const hasRetainedInput = elements.formalBriefText.value || elements.formalBriefFiles.files.length
    || elements.formalBriefUpload.dataset.editFactField || input.replyQuestionId || input.answerRequest
    || Object.values(input.questionAnswers || {}).some(value => value?.trim())
    || Object.values(input.details || {}).some(value => value?.trim());
  if (!input.sourcePanelInteracted && !hasRetainedInput) sourcePanel.open = !confirmed;
  const continueLink = document.querySelector("#brief-enter-creative");
  continueLink.hidden = !confirmed;
  continueLink.href = formalPath("script", projectId);
  elements.formalBriefConfirm.hidden = confirmed;
  const nextStep = document.querySelector("#brief-next-step");
  elements.formalBriefWorkbench.prepend(nextStep);
  document.querySelector("#brief-template-panel").classList.remove("brief-template-pending");
  elements.formalConflictList.closest(".workbench-panel").classList.toggle("brief-no-conflicts", !unresolved);
  elements.formalBriefReadiness.textContent = confirmed ? "已确认" : failed ? "资料待处理" : unresolved ? "待核对冲突" : routing.blocked || standardsBlocked ? "需求待补充" : "可确认";
  elements.formalBriefConfirm.disabled = confirmed || failed || unresolved || standardsBlocked || routing.blocked;
  elements.formalBriefGateTitle.textContent = confirmed ? "需求已确认，可以开始创作" : failed ? "有资料未读完" : unresolved ? "请核对需求冲突" : routing.blocked ? "请补充项目类型" : standardsBlocked ? `请补充${standard.missing.map(standardFieldLabel).join("、") || "创作标准"}` : "需求已整理，请核对后确认";
  elements.formalBriefGateCopy.textContent = confirmed ? `资料与创作标准卡已确认；生成将采用${response.routing?.script_type || "已预检"}模板。` : failed ? "存在读取失败，请先处理失败文件。" : unresolved ? "请确认冲突事实后继续。" : routing.blocked ? (response.routing?.message || "当前资料还不能确定生成模板。") : standard.unavailable ? "请重新导入资料生成标准卡后再确认。" : standard.missing.length ? `请补充${standard.missing.map(standardFieldLabel).join("、")}后再确认。` : `资料与创作标准卡已准备好；确认后将采用${response.routing.script_type}模板。`;
  const params = new URLSearchParams(window.location.search);
  if (params.get("return_to") === "plan") {
    const back = formalPath("script", projectId, {panel: "structure", creative: params.get("creative") || ""});
    elements.formalBriefGateCopy.insertAdjacentHTML("beforeend", ` <a href="${back}" data-brief-return>返回原规划，按当前资料重新核对</a>`);
    continueLink.href = back;
    continueLink.textContent = "返回规划";
  } else continueLink.textContent = "进入创作";
  const missingFields = [...(routing.blocked ? ["脚本类型"] : []), ...standard.missing.map(field => ({tone_keywords: "风格调性", expression_focus: "视频目标"})[field]).filter(Boolean)];
  if (missingFields.length && !confirmed) elements.formalBriefGateCopy.insertAdjacentHTML("beforeend", ` <button class="text-button" type="button" data-brief-fill-missing="${escapeHtml(missingFields.join(","))}">集中补充</button>`);
  renderBriefConversation(projectId);
  const retainedField = formalBriefInput(projectId);
  const retainedDraft = retainedField.drafts[retainedField.editingField];
  const remoteFact = facts.find(fact => fact.label === retainedField.editingField);
  if (remoteFact && retainedDraft?.baseline !== undefined && retainedDraft.baseline !== String(formalValue(remoteFact.value)).trim()) {
    elements.formalBriefSelection.textContent += " 此字段的服务端内容已变化；你的草稿已保留，请对照当前事实后提交。";
    document.querySelector("#brief-source-panel").open = true;
  }
  if (confirmed && params.get("return_to") === "materials" && params.get("next") === "plan") {
    const query = { return_to: "plan", ...(params.get("mode") === "text-only" ? { mode: "text-only" } : {}) };
    elements.formalBriefGateCopy.insertAdjacentHTML("beforeend", ` <a href="${formalPath("materials", projectId, query)}" data-brief-return>返回项目素材</a>`);
  }
  const inlineButton = [...elements.formalFactList.querySelectorAll("[data-edit-formal-fact]")].find(button => button.dataset.editFormalFact === retainedField.editingField);
  if (inlineButton && retainedDraft) renderInlineFormalFact(inlineButton, retainedField);
}

function setFormalPlanState(next, copy) {
  state.formalPlanState = next;
  elements.formalPlanState.textContent = ({
    idle: "等待生成规划",
    loading: "正在读取规划…",
    blocked: "暂不能生成规划",
    waiting_user: "等待你的确认",
    generating: "正在创建脚本版本…",
    completed: "脚本版本已创建",
  })[next] || "正在读取规划…";
  elements.formalPlanStatusCopy.textContent = copy;
}

function formalPlanCollectionPath(projectId) {
  return `/api/formal/projects/${encodeURIComponent(projectId)}/plans?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
}

function formalPlanPreviewPath(projectId, planId, projectVersion) {
  return `/api/formal/projects/${encodeURIComponent(projectId)}/plans/${encodeURIComponent(planId)}?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(projectVersion)}`;
}

function formalPlanConflictPath(projectId, planId, action) {
  return `/api/formal/projects/${encodeURIComponent(projectId)}/plans/${encodeURIComponent(planId)}/conflict-resolutions/${encodeURIComponent(action)}?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
}

function planSourceLabel(source) {
  if (!source || typeof source !== "object") return "来源位置暂未提供";
  const file = formalBusinessSourceName(source);
  const sheet = source.sheet || source.source_sheet, row = source.row || source.source_row;
  if (file === "创意库素材图片" && sheet === "media_assets") return `${file} · 仅有素材来源`;
  const parts = [source.page && `第 ${source.page} 页`, sheet && `${sheet} Sheet`, row && `第 ${row} 行`, source.slide && `第 ${source.slide} 张`, source.image_index && `图片 ${source.image_index}`].filter(Boolean);
  return [file, ...parts].filter(Boolean).join(" · ") || "来源位置暂未提供";
}

function formalScriptPath(projectId, action = "") {
  const suffix = action ? `/${encodeURIComponent(action)}` : "";
  return `/api/formal/projects/${encodeURIComponent(projectId)}/script${suffix}?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
}

function formalScriptDiffPath(projectId, versionNo) {
  return `/api/formal/projects/${encodeURIComponent(projectId)}/script/versions/${encodeURIComponent(versionNo)}/diff?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
}

function formalRequestKey(prefix) {
  const value = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${value}`;
}

function setFormalScriptStatus(status, copy) {
  const labels = {
    loading: "正在读取",
    ready: "已保存版本",
    editing: "有待保存变化",
    busy: "正在暂存草稿",
    completed: "新版本已保存",
    blocked: "暂不可编辑",
    error: "操作未完成",
  };
  elements.formalScriptState.textContent = labels[status] || "脚本与版本";
  elements.formalScriptSaveState.dataset.state = ["error", "blocked"].includes(status) ? "error" : status === "busy" ? "busy" : "ready";
  elements.formalScriptSaveState.textContent = copy;
  syncCreativeNextAction();
}

function formalScriptHasPendingWrites() {
  return state.formalScriptComposingControls.size > 0
    || state.formalScriptDirtyCells.size > 0
    || state.formalScriptRejectedCells.size > 0
    || state.formalScriptSaveRetryPending !== null
    || state.formalScriptSaveInFlight;
}

function formalScriptCellValue(control) {
  return control.dataset.scriptField === "duration_seconds"
    ? Number(control.value)
    : control.value.trim();
}

function syncFormalScriptSaveControls(model) {
  const empty = !model || model.state === "empty";
  elements.formalScriptRevision.textContent = model?.can_save_version || creativeChangedImageShots().length
    ? `已暂存 · 待保存版本`
    : "已保存版本";
  elements.formalScriptUndo.disabled = state.formalScriptBusy || formalScriptHasPendingWrites() || !model?.can_undo;
  elements.formalScriptRedo.disabled = state.formalScriptBusy || formalScriptHasPendingWrites() || !model?.can_redo;
  elements.formalScriptDiscard.disabled = state.formalScriptBusy || state.formalScriptDiscarding || state.formalScriptSaveInFlight
    || (!formalScriptHasPendingWrites() && !model?.can_save_version && !creativeChangedImageShots().length && !sessionStorage.getItem(`mplus-discard-all:${state.agentWorkspaceId}:${formalRoute().projectId}`));
  elements.formalScriptRetry.hidden = !state.formalScriptSaveRetryPending || Boolean(state.formalScriptSaveConflict);
  elements.formalScriptRetry.textContent = "核实原请求并继续保存";
  elements.formalScriptRetry.disabled = state.formalScriptBusy || state.formalScriptSaveInFlight
    || !formalScriptHasPendingWrites();
  elements.formalScriptSave.disabled = state.formalScriptBusy || empty || (!model?.can_save_version && !formalScriptHasPendingWrites() && !creativeChangedImageShots().length && !sessionStorage.getItem(`mplus-version-confirm:${state.agentWorkspaceId}:${formalRoute().projectId}`));
  const openSave = document.querySelector("#creative-open-save");
  if (openSave) openSave.disabled = elements.formalScriptSave.disabled;
  elements.formalScriptReviewWorkbook.disabled = state.formalScriptBusy || formalScriptHasPendingWrites() || empty || model?.state !== "ready";
  syncCreativeNextAction();
  renderFormalScriptSaveConflict();
  if (!formalScriptHasPendingWrites() && formalRoute().projectId === state.formalScriptProjectId && formalSurface() === "plan" && !elements.creativeCurrentStructure.hidden) renderCreativeCurrentStructure(model);
}

function queueFormalScriptCellSave(event) {
  const control = event.target.closest("[data-script-shot][data-script-field]");
  if (!control || !state.formalScript || state.formalScriptDiscarding) return;
  if (event.isComposing) state.formalScriptComposingControls.add(control);
  const shotNo = control.dataset.scriptShot;
  const shotId = control.dataset.scriptShotKey;
  const field = control.dataset.scriptField;
  const shot = (state.formalScript.shots || []).find((item) => String(item.shot_no) === shotNo);
  if (!shot || !shotId || formalScriptShotIdentity(shot) !== shotId) return;
  const value = formalScriptCellValue(control);
  if (["visual", "camera_type", "shot_size", "camera_move"].includes(field) && shot[field] !== value) {
    const imageShot = state.formalImageWorkspace?.shots?.find(item => item.shot_id === shotId);
    if (imageShot?.explicitly_changed) {
      imageShot.confirmed = false; imageShot.needs_recheck = true;
      const index = state.formalImageWorkspace.shots.indexOf(imageShot);
      const checkbox = elements.formalImageShotGrid.querySelector(`[data-image-shot-confirm="${index}"]`);
      if (checkbox) checkbox.checked = false;
      queueCreativeProgress();
    }
  }
  if (shot[field] !== value) { invalidateFormalScriptProposal(); invalidateFormalImageVersionProposal(); }
  const key = `${shotId}:${field}`;
  const rejected = state.formalScriptRejectedCells.get(key);
  if (rejected && rejected.value === value) return;
  state.formalScriptRejectedCells.delete(key);
  control.removeAttribute("aria-invalid");
  const previous = state.formalScriptDirtyCells.get(key);
  const inFlight = state.formalScriptSaveInFlightPending;
  const retryPending = state.formalScriptSaveRetryPending;
  const sameCellPending = inFlight?.key === key || retryPending?.key === key;
  const sentPending = inFlight?.key === key ? inFlight : retryPending?.key === key ? retryPending : null;
  if (sentPending && value === sentPending.value) {
    state.formalScriptDirtyCells.delete(key);
  } else if (shot[field] === value && !sameCellPending) {
    state.formalScriptDirtyCells.delete(key);
  } else if (!sameCellPending || value !== sentPending?.value) {
    const idempotencyKey = previous?.value === value
      ? previous.idempotency_key
      : formalRequestKey("script-cell");
    state.formalScriptDirtyCells.set(key, {
      shot_no: shotNo, shot_id: shotId, field, value, idempotency_key: idempotencyKey,
      expected_project_version: previous?.expected_project_version ?? Number(state.formalScript.project_version || 0),
    });
  }
  elements.formalScriptRevisionProposal.hidden = true;
  if (!state.formalScriptDirtyCells.size) {
    updateFormalScriptSaveStatus();
    syncFormalScriptSaveControls(state.formalScript);
    return;
  }
  setFormalScriptStatus("editing", "当前输入尚未暂存，暂停输入后自动暂存到草稿。");
  syncFormalScriptSaveControls(state.formalScript);
  window.clearTimeout(state.formalScriptSaveTimer);
  state.formalScriptSaveTimer = state.formalScriptComposingControls.size ? null
    : window.setTimeout(() => flushFormalScriptChanges(), 700);
  renderCreativeOutline();
  syncCreativeVisibility();
}

async function flushFormalScriptChanges({ allowRetry = false } = {}) {
  window.clearTimeout(state.formalScriptSaveTimer);
  state.formalScriptSaveTimer = null;
  if (state.formalScriptComposingControls.size) return false;
  if (state.formalScriptSavePromise) return state.formalScriptSavePromise;
  if (state.formalScriptSaveConflict) return false;
  if (state.formalScriptSaveRetryPending && !allowRetry) return false;
  if (!state.formalScriptSaveRetryPending && !state.formalScriptDirtyCells.size) {
    updateFormalScriptSaveStatus();
    return !state.formalScriptRejectedCells.size;
  }
  const projectId = state.formalScriptProjectId;
  if (!projectId || !state.formalScript) return false;
  const generation = state.formalScriptSaveGeneration;
  const active = () => generation === state.formalScriptSaveGeneration && projectId === state.formalScriptProjectId;
  state.formalScriptSavePromise = (async () => {
    state.formalScriptSaveInFlight = true;
    syncFormalScriptSaveControls(state.formalScript);
    while ((allowRetry && state.formalScriptSaveRetryPending) || state.formalScriptDirtyCells.size) {
      if (state.formalScriptComposingControls.size) return false;
      const retrying = state.formalScriptSaveRetryPending;
      let key;
      let pending;
      if (retrying) {
        key = retrying.key;
        pending = retrying;
        state.formalScriptSaveRetryPending = null;
      } else {
        [key, pending] = state.formalScriptDirtyCells.entries().next().value;
        state.formalScriptDirtyCells.delete(key);
      }
      // A retry is the original operation, including its original revisions.
      const requestPending = retrying || {
        ...pending,
        key,
        expected_revision: Number(state.formalScript.revision || 0),
        expected_project_version: Number(state.formalScript.project_version || 0),
      };
      state.formalScriptSaveInFlightPending = requestPending;
      setFormalScriptStatus("busy", "正在保存到服务端，请稍候…");
      try {
        const model = await api(formalScriptPath(projectId, "table"), {
          method: "POST",
          body: JSON.stringify({
            expected_revision: requestPending.expected_revision,
            expected_project_version: requestPending.expected_project_version,
            shot_no: requestPending.shot_no,
            shot_id: requestPending.shot_id,
            field: requestPending.field,
            value: requestPending.value,
            idempotency_key: requestPending.idempotency_key,
          }),
        });
        if (!active()) return false;
        state.formalScript = model;
      } catch (error) {
        if (!active()) return false;
        const code = error.payload?.detail?.code;
        const rejected = error.status === 422 || ["INVALID_INPUT", "DRAFT_INVALID", "SCRIPT_FIELD_CLEAR_FORBIDDEN"].includes(code);
        if (rejected && (!requestPending.result_unknown || code === "INVALID_INPUT" || error.status === 422)) {
          // This request is known not to have written. A newer value owns the cell.
          if (!state.formalScriptDirtyCells.has(key)) {
            const label = formalScriptFieldLabels[pending.field] || pending.field;
            const message = code === "INVALID_INPUT" || error.status === 422
              ? pending.field === "duration_seconds" ? "时长必须为 1 至 3600 秒的整数。"
                : `${label}须为最多 4000 字的文字${["voiceover", "subtitle", "notes"].includes(pending.field) ? "，可以留空" : "，不能为空"}。`
              : `${label}：${formalError(error)}`;
            state.formalScriptRejectedCells.set(key, {...requestPending, message});
            elements.formalScriptTable.querySelector(`[data-script-shot-key="${CSS.escape(pending.shot_id)}"][data-script-field="${CSS.escape(pending.field)}"]`)?.setAttribute("aria-invalid", "true");
          }
          continue;
        }
        state.formalScriptSaveRetryPending = {...requestPending};
        if (["DRAFT_REVISION_CONFLICT", "PROJECT_VERSION_CONFLICT", "DRAFT_NOT_CHANGED"].includes(code) && !requestPending.result_unknown) {
          state.formalScriptSaveConflict = {baseVersionId: state.formalScript.base_version_id, remote: null};
        } else {
          // Network/auth/unknown failures may follow a completed domain write.
          state.formalScriptSaveRetryPending.result_unknown = true;
        }
        state.formalScriptSaveError = true;
        updateFormalScriptSaveStatus();
        return false;
      } finally {
        if (active() && state.formalScriptSaveInFlightPending === requestPending) state.formalScriptSaveInFlightPending = null;
      }
    }
    updateFormalScriptSaveStatus();
    return !state.formalScriptRejectedCells.size;
  })().finally(() => {
    if (!active()) return;
    state.formalScriptSaveInFlight = false;
    state.formalScriptSavePromise = null;
    syncFormalScriptSaveControls(state.formalScript);
  });
  return state.formalScriptSavePromise;
}

const formalScriptFieldLabels = {duration_seconds:"时长", visual:"画面", sound:"声音", voiceover:"旁白", subtitle:"字幕", notes:"备注", structure:"结构", camera_type:"机位", shot_size:"景别", camera_move:"运镜"};

function updateFormalScriptSaveStatus() {
  const rejected = [...state.formalScriptRejectedCells.values()];
  state.formalScriptSaveError = Boolean(rejected.length || state.formalScriptSaveRetryPending);
  if (state.formalScriptSaveConflict) {
    setFormalScriptStatus("error", "稿件已在另一处更新。此处输入已保留，请读取最新内容并核对差异。");
  } else if (state.formalScriptSaveRetryPending) {
    setFormalScriptStatus("error", "上次保存结果待核实，输入已保留。请确认登录有效后，核实原请求并继续保存。");
  } else if (rejected.length) {
    setFormalScriptStatus("error", `${rejected.map(item => `第 ${item.shot_no} 镜：${item.message}`).join("；")} 请改正对应输入，其他有效修改可继续保存。`);
  } else {
    setFormalScriptStatus("editing", "修改已保存到当前稿；核对后可保存为新版本。");
  }
}

function formalScriptLocalChanges() {
  const changes = new Map(state.formalScriptRejectedCells);
  if (state.formalScriptSaveRetryPending) changes.set(state.formalScriptSaveRetryPending.key, state.formalScriptSaveRetryPending);
  for (const [key, value] of state.formalScriptDirtyCells) changes.set(key, value);
  return changes;
}

function renderFormalScriptSaveConflict() {
  const panel = document.querySelector("#formal-script-conflict");
  const conflict = state.formalScriptSaveConflict;
  panel.hidden = !conflict;
  if (!conflict) return;
  const remote = conflict.remote;
  const local = [...formalScriptLocalChanges().values()];
  const sameTargets = remote && remote.base_version_id === conflict.baseVersionId
    && local.every(item => remote.shots.some(shot => shot.shot_id === item.shot_id));
  panel.innerHTML = `<p>此处未保存的输入已保留。${remote ? "请核对以下差异，再决定使用哪份内容。" : "先读取最新内容，不会自动覆盖任一窗口。"}</p>
    ${remote ? local.map(item => { const shot = remote.shots.find(shot => shot.shot_id === item.shot_id);
      return `<p>第 ${escapeHtml(item.shot_no)} 镜${escapeHtml(formalScriptFieldLabels[item.field] || item.field)}：此处输入「${escapeHtml(String(item.value))}」；已保存「${escapeHtml(String(shot?.[item.field] ?? "镜头已不可用"))}」</p>`; }).join("") : ""}
    ${remote && !sameTargets ? "<p>基础版本或镜头身份已变化，请复制保留所需文字后使用已保存内容，不能直接套用旧修改。</p>" : ""}
    <button type="button" class="secondary-button compact" data-script-conflict-read>读取最新内容</button>
    ${sameTargets ? '<button type="button" class="secondary-button compact" data-script-conflict-local>保留此处修改并保存</button>' : ""}
    ${remote ? '<button type="button" class="secondary-button compact" data-script-conflict-server>使用已保存内容，放弃此处未保存修改</button>' : ""}`;
}

async function resolveFormalScriptSaveConflict(action) {
  const conflict = state.formalScriptSaveConflict;
  if (!conflict) return;
  const generation = state.formalScriptSaveGeneration;
  const projectId = state.formalScriptProjectId;
  if (action === "read") {
    try {
      const remote = await api(formalScriptPath(projectId));
      if (generation !== state.formalScriptSaveGeneration || state.formalScriptSaveConflict !== conflict) return;
      conflict.remote = remote;
      renderFormalScriptSaveConflict();
    } catch (error) {
      if (generation === state.formalScriptSaveGeneration) setFormalScriptStatus("error", `${formalError(error)} 此处输入已保留，可重试读取最新内容。`);
    }
    return;
  }
  const remote = conflict.remote;
  if (!remote) return;
  const local = formalScriptLocalChanges();
  if (action === "local" && (remote.base_version_id !== conflict.baseVersionId
      || [...local.values()].some(item => !remote.shots.some(shot => shot.shot_id === item.shot_id)))) return;
  state.formalScript = remote;
  state.formalScriptSaveRetryPending = null;
  state.formalScriptSaveConflict = null;
  state.formalScriptRejectedCells.clear();
  state.formalScriptDirtyCells.clear();
  state.formalScriptComposingControls.clear();
  state.formalScriptSaveError = false;
  if (action === "local") {
    for (const [key, item] of local) {
      const shot = remote.shots.find(shot => shot.shot_id === item.shot_id);
      if (shot[item.field] !== item.value) state.formalScriptDirtyCells.set(key, {
        shot_no: shot.shot_no, shot_id: shot.shot_id, field: item.field, value: item.value,
        idempotency_key: formalRequestKey("script-cell"), expected_project_version: remote.project_version,
      });
    }
  }
  renderFormalScript(remote);
  if (action === "local") await flushFormalScriptChanges();
}

function formalScriptDate(value) {
  const date = new Date(String(value || ""));
  if (Number.isNaN(date.getTime())) return "保存时间待确认";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function formalScriptDiffValue(value) {
  if (value === null || value === undefined || value === "") return "（空）";
  return String(value);
}

function renderFormalScriptDiff(model, { loading = false, error = "" } = {}) {
  if (loading) {
    elements.formalScriptDiff.innerHTML = '<div class="workbench-empty">正在核对版本变化与未变化范围…</div>';
    return;
  }
  if (error) {
    elements.formalScriptDiff.innerHTML = `<div class="ui00-business-empty"><strong>版本差异暂不可用</strong><p>${escapeHtml(error)}</p></div>`;
    return;
  }
  if (!model) {
    elements.formalScriptDiff.innerHTML = '<div class="workbench-empty">保存新版本后，可在这里核对改前、改后和未变化范围。</div>';
    return;
  }
  const changes = Array.isArray(model.changes) ? model.changes : [];
  const checks = Array.isArray(model.verification_items) ? model.verification_items : [];
  elements.formalScriptDiff.innerHTML = `
    <div class="ui00-script-diff-head">
      <div><span class="eyebrow">版本对比</span><h3 id="formal-script-diff-title" tabindex="-1">${escapeHtml(model.comparison_label || "版本变化核对")}</h3></div>
      <p>${escapeHtml(`${model.changed_shot_count || 0} 个镜头 · ${model.changed_field_count || 0} 项变化 · ${model.review_status || "等待项目经理审核"}`)}</p>
    </div>
    <div class="ui00-script-diff-list">
      ${changes.map((item) => `<article class="ui00-script-diff-item"><div class="ui00-script-diff-label">镜头 ${escapeHtml(item.shot_no)}<br>${escapeHtml(formalBusinessFieldLabel(item.field))}</div><del class="ui00-script-diff-value before"><b>修改前</b><br>${escapeHtml(formalScriptDiffValue(item.before))}</del><span class="ui00-script-diff-arrow" aria-hidden="true">→</span><ins class="ui00-script-diff-value"><b>修改后</b><br>${escapeHtml(formalScriptDiffValue(item.after))}</ins></article>`).join("")}
    </div>
    <ul class="ui00-script-verification" aria-label="未变化范围核对结果">${checks.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

async function loadFormalScriptDiff(versionNo, { focus = false } = {}) {
  const projectId = formalRoute().projectId;
  if (!projectId || state.formalScriptDiffBusy) return;
  const epoch = state.formalScriptEpoch;
  state.formalScriptDiffBusy = true;
  renderFormalScriptDiff(null, { loading: true });
  try {
    const model = await api(formalScriptDiffPath(projectId, versionNo));
    if (epoch !== state.formalScriptEpoch || formalRoute().page !== "script") {
      state.formalScriptDiffBusy = false;
      return;
    }
    state.formalScriptDiff = model;
    state.formalScriptDiffBusy = false;
    renderFormalScriptDiff(model);
    elements.formalScriptVersions.querySelectorAll("[data-script-version-diff]").forEach((button) => {
      button.setAttribute("aria-expanded", String(Number(button.dataset.scriptVersionDiff) === Number(versionNo)));
    });
    if (focus) elements.formalScriptDiff.querySelector("#formal-script-diff-title")?.focus({ preventScroll: true });
  } catch (error) {
    if (epoch !== state.formalScriptEpoch || formalRoute().page !== "script") {
      state.formalScriptDiffBusy = false;
      return;
    }
    state.formalScriptDiff = null;
    state.formalScriptDiffBusy = false;
    renderFormalScriptDiff(null, { error: formalError(error) });
  }
}

const creativeOptionalFields = new Map();
function formalScriptControl(shot, field, label, { number = false } = {}) {
  const key = `${shot.shot_id}:${field}`;
  const pending = formalScriptLocalChanges().get(key) || (state.formalScriptSaveInFlightPending?.key === key ? state.formalScriptSaveInFlightPending : null);
  const value = pending?.value ?? shot[field] ?? "";
  const attributes = `data-script-shot="${escapeHtml(shot.shot_no)}" data-script-shot-key="${escapeHtml(formalScriptShotIdentity(shot))}" data-script-field="${field}" aria-label="第 ${escapeHtml(shot.shot_no)} 镜${label}" autocomplete="off" ${state.formalScriptBusy ? "disabled" : ""}`;
  if (number) return `<input class="ui00-script-cell" type="number" min="1" max="3600" step="1" value="${escapeHtml(value)}" ${attributes} />`;
  const control = `<textarea class="ui00-script-cell" rows="2" maxlength="4000" ${attributes}>${escapeHtml(value)}</textarea>`;
  if (!["voiceover", "subtitle", "notes"].includes(field)) return control;
  const detailKey = `${state.formalScriptProjectId}:${key}`;
  if (!creativeOptionalFields.has(detailKey)) creativeOptionalFields.set(detailKey, Boolean(String(value).trim()));
  const expanded = creativeState().editMode === "table" || creativeOptionalFields.get(detailKey);
  return `<details class="creative-optional-field" data-optional-field="${escapeHtml(detailKey)}" ${expanded ? "open" : ""}><summary>${escapeHtml(label)} · ${escapeHtml(String(value).trim().slice(0, 60) || "按需填写")}</summary>${control}</details>`;
}

function formalScriptShotIdentity(shot) {
  return String(shot?.shot_id || "").trim();
}

function formalScriptShotMeta(shot) {
  const values = [
    ["机位", shot.camera_type],
    ["景别", shot.shot_size],
    ["运镜", shot.camera_move],
  ].filter((item) => item[1]);
  return values.length
    ? `<dl class="ui00-script-shot-meta">${values.map((item) => `<div><dt>${escapeHtml(item[0])}</dt><dd>${escapeHtml(item[1])}</dd></div>`).join("")}</dl>`
    : '<span class="ui00-script-meta-empty">拍摄属性待补充</span>';
}

function renderFormalScriptProtection(model) {
  const shots = Array.isArray(model?.shots) ? model.shots : [];
  const locked = shots.filter((shot) => Boolean(shot.assistant_locked));
  elements.formalScriptLockSummary.textContent = locked.length
    ? `已锁定 ${locked.length} 镜：${locked.map((shot) => shot.shot_no).join("、")}`
    : "尚未锁定镜头";
  elements.formalScriptLockEdit.hidden = state.formalScriptLockEditing;
  elements.formalScriptLockClear.hidden = state.formalScriptLockEditing || locked.length === 0;
  elements.formalScriptLockSave.hidden = !state.formalScriptLockEditing;
  elements.formalScriptLockCancel.hidden = !state.formalScriptLockEditing;
  elements.formalScriptLockEdit.disabled = state.formalScriptBusy || shots.length === 0;
  elements.formalScriptLockClear.disabled = state.formalScriptBusy;
  elements.formalScriptLockSave.disabled = state.formalScriptBusy;
}

function renderFormalScript(model, focusTarget = null) {
  state.formalScript = model;
  const basisNotice = document.querySelector("#formal-script-basis-notice");
  basisNotice.hidden = !model?.has_new_confirmed_brief;
  basisNotice.querySelector("a").href = formalPath("script", formalRoute().projectId, { panel: "structure" });
  const empty = !model || model.state === "empty";
  elements.formalScriptProjectName.textContent = model?.project_name || "当前项目";
  elements.formalScriptVersionLabel.textContent = model?.version_label || "尚无脚本版本";
  syncFormalScriptSaveControls(model);
  elements.formalScriptInstruction.disabled = state.formalScriptBusy || empty;
  elements.formalScriptInstructionSubmit.disabled = state.formalScriptBusy || empty;
  elements.formalScriptSaveReason.disabled = state.formalScriptBusy || empty;
  const shots = Array.isArray(model?.shots) ? model.shots : [];
  renderFormalScriptProtection(model);
  const pending = Array.isArray(model?.pending_confirmations) ? model.pending_confirmations : [];
  renderCreativePendingIssues();
  elements.formalScriptTable.innerHTML = shots.length ? `
    <table class="ui00-script-table"><thead><tr><th scope="col">镜头</th><th scope="col">拍摄属性</th><th scope="col">结构</th><th scope="col">时长</th><th scope="col">画面</th><th scope="col">声音</th><th scope="col">旁白</th><th scope="col">字幕</th><th scope="col">备注</th><th scope="col">来源</th></tr></thead><tbody>
      ${shots.map((shot) => {
        const shotId = formalScriptShotIdentity(shot);
        const lockControl = state.formalScriptLockEditing
          ? `<label class="ui00-script-lock-choice"><input type="checkbox" value="${escapeHtml(shotId)}" data-script-lock-shot="${escapeHtml(shotId)}"${shot.assistant_locked ? " checked" : ""}${shotId && !state.formalScriptBusy ? "" : " disabled"} /><span>锁定</span></label>`
          : shot.assistant_locked ? '<span class="ui00-script-lock-badge">已锁定</span>' : "";
        const reconfirm = shot.image_reconfirmation_required ? '<span class="ui00-script-reconfirm">图片需重新确认</span>' : "";
        return `<tr data-script-shot-id="${escapeHtml(shotId)}"><td><strong class="ui00-script-shot-number">${escapeHtml(shot.shot_no)}</strong>${lockControl}${reconfirm}</td><td data-field-label="拍摄属性">${formalScriptShotMeta(shot)}</td><td data-field-label="结构">${formalScriptControl(shot, "structure", "结构")}</td><td data-field-label="时长">${formalScriptControl(shot, "duration_seconds", "时长", { number: true })}</td><td data-field-label="画面">${formalScriptControl(shot, "visual", "画面")}</td><td data-field-label="声音">${formalScriptControl(shot, "sound", "声音")}</td><td data-field-label="旁白">${formalScriptControl(shot, "voiceover", "旁白")}</td><td data-field-label="字幕">${formalScriptControl(shot, "subtitle", "字幕")}</td><td data-field-label="备注">${formalScriptControl(shot, "notes", "备注")}</td><td>${shot.adaptation?.origin === "original_suggestion" ? '<span class="ui00-script-reconfirm">原创建议</span>' : ""}<details class="ui00-script-source"><summary>查看来源</summary><p>${escapeHtml(planSourceLabel(shot.source))}</p>${shot.adaptation?.summary ? `<p>${escapeHtml(shot.adaptation.summary)}</p>` : ""}</details></td></tr>`;
      }).join("")}
    </tbody></table>` : '<div class="ui00-business-empty"><strong>当前项目还没有脚本版本</strong><p>请先在规划与生成中确认规划并创建首个脚本版本。</p></div>';
  const versions = Array.isArray(model?.versions) ? model.versions : [];
  elements.formalScriptVersions.innerHTML = versions.length ? versions.map((version) => `<article class="ui00-script-version-item ${version.is_current ? "is-current" : ""}"><strong>${escapeHtml(version.label || "脚本版本")}${version.is_current ? " · 当前" : ""}</strong><span>${escapeHtml(version.change_summary || "版本变化待核对")}</span><span>${escapeHtml(version.review_status || "等待项目经理审核")} · ${escapeHtml(formalScriptDate(version.created_at))}</span><a class="secondary-button compact" data-formal-target href="${formalPath("review", formalRoute().projectId, { version_no: version.version_no })}">查看本版审核与交付</a><button class="secondary-button compact" type="button" data-script-version-workbook="${escapeHtml(version.version_no)}">下载本版复核稿</button>${version.has_diff ? `<button class="secondary-button compact" type="button" data-script-version-diff="${escapeHtml(version.version_no)}" aria-expanded="${String(Number(state.formalScriptDiff?.version_no) === Number(version.version_no))}" aria-controls="formal-script-diff">核对变化</button>` : ""}</article>`).join("") : '<div class="workbench-empty">尚无正式版本。</div>';
  renderFormalScriptDiff(state.formalScriptDiff);
  if (empty) setFormalScriptStatus("blocked", model?.message || "请先创建首个脚本版本。");
  else if (!state.formalScriptBusy) setFormalScriptStatus(model.can_save_version ? "editing" : "ready", model.can_save_version ? `草稿已自动保存。核对后可保存为新版本。` : "当前版本已保存，可直接编辑。");
  if (!state.formalScriptBusy && state.formalScriptSaveError) updateFormalScriptSaveStatus();
  if (focusTarget) {
    const selector = `[data-script-shot="${CSS.escape(String(focusTarget.shot_no))}"][data-script-field="${CSS.escape(String(focusTarget.field))}"]`;
    elements.formalScriptTable.querySelector(selector)?.focus({ preventScroll: true });
  }
  syncCreativeContext(model);
  syncCreativeShotVisibility();
}

async function loadFormalScript(projectId) {
  const epoch = ++state.formalScriptEpoch;
  document.querySelector("#formal-script-basis-notice").hidden = true;
  document.querySelector("#script-task-context").hidden = true;
  state.formalScriptSaveGeneration += 1;
  const available = Boolean(projectId && state.formalScriptAvailable && state.agentAuth && state.agentWorkspaceId);
  elements.formalScriptProjectRequired.hidden = available;
  elements.formalScriptWorkbench.hidden = !available;
  state.formalScript = null;
  state.formalScriptReadError = '';
  state.formalScriptProjectId = projectId || "";
  state.formalScriptDirtyCells.clear();
  state.formalScriptComposingControls.clear();
  state.formalScriptSaveRetryPending = null;
  state.formalScriptRejectedCells.clear();
  state.formalScriptSaveConflict = null;
  state.formalScriptSavePromise = null;
  state.formalScriptSaveInFlight = false;
  state.formalScriptSaveInFlightPending = null;
  state.formalScriptDiscarding = false;
  window.clearTimeout(state.formalScriptSaveTimer);
  state.formalScriptSaveTimer = null;
  state.formalScriptDiff = null;
  state.formalScriptDiffBusy = false;
  state.formalScriptSaveError = false;
  state.formalScriptLockEditing = false;
  state.formalScriptScopeTask = null;
  invalidateFormalScriptProposal();
  state.formalScriptProposalInstruction = "";
  state.formalScriptBusy = available;
  renderCreativeOutline();
  syncCreativeShotVisibility();
  if (!available) {
    setFormalScriptStatus("blocked", projectId ? "脚本编辑服务尚未接入。" : "请先从项目列表进入项目。");
    return;
  }
  setFormalScriptStatus("loading", "正在读取当前脚本、草稿和版本记录…");
  elements.formalScriptTable.innerHTML = '<div class="ui00-business-empty"><strong>正在读取当前脚本</strong><p>请稍候。</p></div>';
  try {
    const model = await api(formalScriptPath(projectId));
    if (epoch !== state.formalScriptEpoch || formalRoute().page !== "script") return;
    state.formalScriptBusy = false;
    if (creativeState().projectId === projectId && !creativeState().loaded) restoreCreativeInputs(model);
    renderFormalScript(model);
    await loadScriptTaskContext(projectId, epoch);
    if (epoch !== state.formalScriptEpoch || formalRoute().projectId !== projectId) return;
    const analysisTaskId = new URLSearchParams(window.location.search).get("analysis_task_id");
    if (analysisTaskId) {
      const task = await api(`/api/agent/tasks/${encodeURIComponent(analysisTaskId)}`);
      if (epoch !== state.formalScriptEpoch || formalRoute().projectId !== projectId) return;
      const response = task.response || task.task?.response || {};
      if (response.instruction) elements.formalScriptInstruction.value = response.instruction;
      renderFormalScriptRevisionProposal({ ...task, task_id: task.task_id || task.task?.task_id || analysisTaskId, response });
      elements.formalScriptRevisionProposal.hidden = false;
    }
    const currentVersion = (model.versions || []).find((version) => version.is_current && version.has_diff);
    if (currentVersion) await loadFormalScriptDiff(currentVersion.version_no);
  } catch (error) {
    if (epoch !== state.formalScriptEpoch || formalRoute().page !== "script") return;
    state.formalScriptBusy = false;
    state.formalScriptReadError = formalError(error);
    elements.creativeShotCount.textContent = "暂不可用";
    elements.creativeShotList.innerHTML = '<li class="creative-outline-empty">镜头暂不可用，请重新读取。</li>';
    elements.formalScriptTable.innerHTML = `<div class="ui00-business-empty"><strong>脚本暂不可用</strong><p>${escapeHtml(formalError(error))}</p></div>`;
    setFormalScriptStatus("error", formalError(error));
  }
}

async function saveFormalScriptLocks(lockedShotIds) {
  const projectId = formalRoute().projectId;
  if (!projectId || !state.formalScript || state.formalScriptBusy) {
    showToast("当前脚本缺少可写项目版本，请刷新后重试。");
    return;
  }
  if (!(await flushFormalScriptChanges())) return;
  const projectVersion = state.formalScript?.project_version;
  if (!Number.isInteger(projectVersion)) {
    showToast("当前脚本缺少可写项目版本，请刷新后重试。");
    return;
  }
  state.formalScriptBusy = true;
  renderFormalScriptProtection(state.formalScript);
  try {
    const model = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/script/assistant-locks?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {
      method: "PUT",
      body: JSON.stringify({
        expected_project_version: projectVersion,
        locked_shot_ids: lockedShotIds,
        idempotency_key: formalRequestKey("script-assistant-locks"),
      }),
    });
    state.formalScriptBusy = false;
    state.formalScriptLockEditing = false;
    if (Array.isArray(model?.shots)) renderFormalScript(model);
    else await loadFormalScript(projectId);
    showToast(lockedShotIds.length ? `已锁定 ${lockedShotIds.length} 个镜头。` : "已解除全部锁定。");
  } catch (error) {
    state.formalScriptBusy = false;
    if (["PROJECT_VERSION_CONFLICT", "DRAFT_VERSION_CONFLICT"].includes(error?.payload?.detail?.code)) {
      state.formalScriptLockEditing = false;
      renderFormalScriptProtection(state.formalScript);
    } else {
      renderFormalScriptProtection(state.formalScript);
    }
    showToast(formalError(error));
  }
}

async function editFormalScriptLocks() {
  if (!state.formalScript || state.formalScriptBusy) return;
  if (!(await flushFormalScriptChanges())) return;
  state.formalScriptLockEditing = true;
  renderFormalScript(state.formalScript);
  elements.formalScriptTable.querySelector("[data-script-lock-shot]")?.focus({ preventScroll: true });
}

function cancelFormalScriptLocks() {
  state.formalScriptLockEditing = false;
  renderFormalScript(state.formalScript);
  elements.formalScriptLockEdit.focus({ preventScroll: true });
}

function persistFormalScriptLocks() {
  const lockedShotIds = Array.from(elements.formalScriptTable.querySelectorAll("[data-script-lock-shot]:checked"))
    .map((input) => input.value)
    .filter(Boolean);
  saveFormalScriptLocks(lockedShotIds);
}

async function runFormalScriptAction(action, body, { focusTarget = null, completed = false } = {}) {
  const projectId = state.formalScriptProjectId || formalRoute().projectId;
  if (!projectId || state.formalScriptBusy || !state.formalScript) return null;
  if (!(await flushFormalScriptChanges())) return null;
  state.formalScriptBusy = true;
  renderFormalScript(state.formalScript);
  setFormalScriptStatus("busy", completed ? "正在校验并保存新的脚本版本…" : "正在保存当前草稿…");
  try {
    const model = await api(formalScriptPath(projectId, action), { method: "POST", body: JSON.stringify(body) });
    state.formalScriptBusy = false;
    if (completed) state.formalScriptDiff = null;
    renderFormalScript(model, focusTarget);
    if (completed) {
      setFormalScriptStatus("completed", "新的不可变脚本版本已保存；旧版本仍保留，尚未发布。");
      elements.formalScriptSaveReason.value = "";
      state.uiInputBaselines.delete(elements.formalScriptSaveReason);
      elements.formalScriptSaveState.focus({ preventScroll: true });
      const currentVersion = (model.versions || []).find((version) => version.is_current && version.has_diff);
      if (currentVersion) await loadFormalScriptDiff(currentVersion.version_no);
    }
    return model;
  } catch (error) {
    state.formalScriptBusy = false;
    const code = error?.payload?.detail?.code;
    if (["PROJECT_VERSION_CONFLICT", "DRAFT_REVISION_CONFLICT", "DRAFT_VERSION_CONFLICT"].includes(code)) {
      renderFormalScript(state.formalScript, focusTarget);
      setFormalScriptStatus("error", formalError(error));
    } else {
      renderFormalScript(state.formalScript, focusTarget);
      setFormalScriptStatus("error", formalError(error));
    }
    elements.formalScriptSaveState.focus({ preventScroll: true });
    return null;
  }
}

async function changeFormalScriptCell(event) {
  queueFormalScriptCellSave(event);
}

function formalScriptProposalValue(value, emptyLabel = "未提供") {
  if (value === null || value === undefined) return emptyLabel;
  if (Array.isArray(value)) return value.map((item) => formalScriptProposalValue(item, emptyLabel)).join("；") || emptyLabel;
  if (typeof value !== "object") return String(value);
  return [
    value.structure, value.visual, value.sound, value.voiceover,
    value.subtitle, value.notes, value.camera_type, value.shot_size, value.camera_move,
  ].filter(Boolean).join("／") || value.label || value.summary || `镜头 ${value.shot_no || "—"}`;
}

function formalScriptProposalChanges(response) {
  if (response.draft_only && Array.isArray(response.suggestions) && response.suggestions.length) {
    const labels = { structure: "结构", visual: "画面", sound: "声音", voiceover: "旁白", subtitle: "字幕", notes: "备注", duration_seconds: "时长", camera_type: "镜头类型", shot_size: "景别", camera_move: "运镜", source: "来源" };
    const changes = response.preview?.changes || response.changes || [];
    return `<div class="creative-shot-suggestions">${response.suggestions.map(suggestion => {
      const id = suggestion.shot_id;
      const fields = changes.filter(change => change.shot_id === id || String(change.shot_no) === String(suggestion.shot_no));
      return `<section class="creative-shot-suggestion"><header><label><input type="checkbox" data-creative-retain-shot="${escapeHtml(id)}" checked>第 ${escapeHtml(suggestion.shot_no)} 镜 · 保留本镜整组建议</label></header><p>${escapeHtml(suggestion.reason || response.shot_reasons?.[id] || "按本次修改要求调整。")}</p><div class="ui00-script-proposal-diff">${fields.map(item => `<article><b>${escapeHtml(labels[item.field] || item.label || "镜头内容")}</b><del>${escapeHtml(formalScriptProposalValue(item.before, "原镜头"))}</del><span aria-hidden="true">→</span><ins>${escapeHtml(formalScriptProposalValue(item.after, "删除整镜"))}</ins></article>`).join("")}</div><small>取消本镜会一并取消其字段、整镜替换与来源调整。</small></section>`;
    }).join("")}</div>`;
  }
  const preview = response.preview || {};
  const changes = (Array.isArray(preview.changes) ? preview.changes : Array.isArray(response.changes) ? response.changes : []).filter(item => !preview.image_changes?.length || item.field !== "image");
  if (changes.length) {
    return `<div class="ui00-script-proposal-diff">${changes.map((item) => `<article><b>${escapeHtml(item.label || item.field || `镜头 ${item.shot_no || "—"}`)}</b><del>${escapeHtml(formalScriptProposalValue(item.before, "整镜"))}</del><span aria-hidden="true">→</span><ins>${escapeHtml(formalScriptProposalValue(item.after, "删除整镜"))}</ins></article>`).join("")}</div>`;
  }
  if (preview.image_changes?.length || preview.before === undefined && preview.after === undefined) return "";
  return `<div class="ui00-script-proposal-diff"><article><b>${escapeHtml(preview.label || `镜头 ${preview.shot_no || "—"}`)}</b><del>${escapeHtml(formalScriptProposalValue(preview.before, "整镜"))}</del><span aria-hidden="true">→</span><ins>${escapeHtml(formalScriptProposalValue(preview.after, "删除整镜"))}</ins></article></div>`;
}

function formalScriptImageChanges(response) {
  const changes = response.preview?.image_changes || [];
  if (!changes.length) return "";
  return `<section class="ui00-script-image-changes" aria-label="配图修改对比">${changes.map((change, index) => `<article><h3>第 ${escapeHtml(change.shot_no || "—")} 镜配图</h3><div class="agent-candidate-grid">${["before", "after"].map(side => `<figure><figcaption>${side === "before" ? "修改前" : "修改后"} · ${escapeHtml(change[`${side}_caption`] || (side === "before" ? "原配图" : "本次配图"))}</figcaption><div data-save-image-index="${index}" data-save-image-side="${side}" role="status">正在读取配图…</div></figure>`).join("")}</div>${change.reuse_reason ? `<p>复用说明：${escapeHtml(change.reuse_reason)}</p>` : ""}<p>审核影响：${escapeHtml(change.review_impact || response.image_impact || "请核对本次修改的审核影响")}</p></article>`).join("")}</section>`;
}

async function loadFormalScriptImageChanges(response, panel) {
  const projectId = formalRoute().projectId;
  const workspace = state.formalImageWorkspace;
  const projectVersion = state.formalScript?.project_version ?? workspace?.project_version;
  const active = () => formalRoute().projectId === projectId && state.formalScriptRevisionProposal?.response === response && panel.isConnected;
  const sameBase = workspace && workspace.base_version_id === response.before?.version_id;
  await Promise.all((response.preview?.image_changes || []).flatMap((change, index) => ["before", "after"].map(async side => {
    const host = panel.querySelector(`[data-save-image-index="${index}"][data-save-image-side="${side}"]`);
    if (!host) return;
    const key = change[`${side}_image_key`] || "";
    const shot = sameBase ? workspace.shots?.find(item => item.shot_id === change.shot_id) : null;
    const reference = side === "before" && !key ? shot?.reference_image : null;
    let url = /^data:image\/(png|jpeg|gif|bmp|webp);base64,/.test(reference?.inline_preview || "") ? reference.inline_preview : "";
    try {
      if (key) {
        const path = `/api/formal/projects/${encodeURIComponent(projectId)}/images/${encodeURIComponent(key)}/preview?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(projectVersion)}`;
        url = await requestFormalPreview(path, { candidate: {image_key:key}, model: workspace || state.formalScript,
          owner: `save-image-comparison:${projectId}`, priority: 0 });
      }
      if (!active() || !host.isConnected) return;
      if (!url) { host.textContent = reference ? "原版本参考图暂时无法读取" : "无图"; return; }
      const image = document.createElement("img");
      image.src = url;
      image.alt = `第 ${change.shot_no} 镜${side === "before" ? "修改前" : "修改后"}配图${reference ? "（版本参考图，未确认）" : ""}`;
      image.style.cssText = "display:block;width:100%;max-width:24rem;aspect-ratio:16/9;object-fit:contain";
      host.replaceChildren(image);
      if (reference) {
        const note = document.createElement("p"); note.textContent = "原版本参考图，未确认"; host.append(note);
      }
    } catch (error) { if (active() && host.isConnected) host.textContent = `配图暂时无法读取：${formalError(error)}`; }
  })));
}

function formalScriptModelEvidence(response) {
  const evidence = response.model_evidence || response.editor_audit || {};
  const facts = [
    ["状态", evidence.status],
    ["处理方式", evidence.provider_label],
    ["模型", evidence.model_label],
    ["调用次数", evidence.call_count],
    ["结果", evidence.summary],
  ].filter((item) => item[1] !== undefined && item[1] !== null && String(item[1]).trim());
  if (!facts.length) return "";
  return `<details class="ui00-script-model-evidence"><summary>查看处理记录</summary><dl>${facts.map((item) => `<div><dt>${escapeHtml(item[0])}</dt><dd>${escapeHtml(item[1])}</dd></div>`).join("")}</dl></details>`;
}

function formalScriptNextHref(response, label = "继续处理") {
  if (!response.next_href) return "";
  return `<a class="secondary-button compact" href="${escapeHtml(response.next_href)}" data-script-next-href="1">${escapeHtml(response.next_label || label)}</a>`;
}

function formalScriptScopeCandidates(response) {
  const candidates = response.suggested_shots || response.suggested_shot_ids || response.scope_options || response.suggested_scope || response.target_candidates || response.suggested_targets || [];
  return (Array.isArray(candidates) ? candidates : []).map((item) => {
    if (typeof item === "string") return { shot_id: item, label: item, selected: true };
    return {
      shot_id: String(item?.shot_id || "").trim(),
      label: item?.label || item?.summary || (item?.shot_no ? `镜头 ${item.shot_no}` : item?.shot_id),
      selected: item?.selected !== false,
    };
  }).filter((item) => item.shot_id);
}

function renderFormalScriptScope(result, response, instruction = "") {
  const candidates = formalScriptScopeCandidates(response);
  const analysisTaskId = response.analysis_task_id || result?.analysis_task_id || result?.task_id || "";
  state.formalScriptScopeTask = analysisTaskId ? {
    analysis_task_id: analysisTaskId,
    instruction: response.instruction || instruction || elements.formalScriptInstruction.value.trim(),
  } : null;
  elements.formalScriptScope.hidden = false;
  elements.formalScriptScopeCopy.textContent = response.summary || "请核对本次建议修改的镜头范围。";
  elements.formalScriptScopeOptions.innerHTML = candidates.length
    ? candidates.map((item) => `<label><input type="checkbox" value="${escapeHtml(item.shot_id)}" data-script-scope-shot="1"${item.selected ? " checked" : ""} /><span>${escapeHtml(item.label || item.shot_id)}</span></label>`).join("")
    : '<p>当前没有可选择的镜头，请按下方指引继续。</p>';
  elements.formalScriptScopeContinue.disabled = !analysisTaskId || candidates.length === 0;
}

function invalidateFormalScriptProposal() {
  state.formalScriptProposalRequest += 1;
  state.formalScriptRevisionProposal = null;
  state.formalScriptScopeTask = null;
  elements.formalScriptScope.hidden = true;
  elements.formalScriptRevisionProposal.hidden = true;
  elements.formalScriptSaveProposal.hidden = true;
  elements.formalScriptInstructionSubmit.hidden = false;
  elements.formalScriptInstructionSubmit.disabled = state.formalScriptBusy || !state.formalScript;
  elements.formalScriptSave.hidden = false;
  syncFormalScriptSaveControls(state.formalScript);
}

function renderFormalScriptRevisionProposal(result, instruction = "") {
  state.formalScriptRevisionProposal = result;
  const response = result?.response || {};
  state.formalScriptProposalInstruction = response.instruction || instruction || elements.formalScriptInstruction.value.trim();
  const savingVersion = response.type === "action_proposal" && !response.draft_only;
  const proposal = savingVersion ? elements.formalScriptSaveProposal : elements.formalScriptRevisionProposal;
  elements.formalScriptRevisionProposal.hidden = savingVersion;
  elements.formalScriptSaveProposal.hidden = !savingVersion;
  elements.formalScriptSave.hidden = savingVersion;
  const responseType = response.type === "needs_clarification" && response.analysis_code === "script_scope_required"
    ? "scope_required"
    : response.type;
  elements.formalScriptScope.hidden = true;
  state.formalScriptScopeTask = null;
  if (response.workbench_parent_task_id || result?.task?.workbench_parent_task_id) {
    proposal.innerHTML = workbenchParentReviewHtml(response.workbench_parent_task_id || result.task.workbench_parent_task_id);
  } else if (responseType === "scope_required") {
    renderFormalScriptScope(result, response, state.formalScriptProposalInstruction);
    proposal.innerHTML = '<strong>选择要修改的镜头，再继续预览。</strong>';
  } else if (responseType === "action_proposal") {
    const preview = response.preview || {};
    const rerun = (response.rerun_nodes || []).length ? "<p>保存时同步更新相关内容。</p>" : "";
    const confirmLabel = response.draft_only ? "应用到草稿" : "确认保存新版本";
    const affectedShots = (response.affected_shots || preview.affected_shots || []).map((item) => formalScriptProposalValue(item));
    proposal.innerHTML = `<strong>${savingVersion ? "保存新版本" : "修改对比"}</strong><p>${escapeHtml(response.summary || "请核对以下改动。")}</p>${formalScriptProposalChanges(response)}${formalScriptImageChanges(response)}<p>影响镜头：${escapeHtml(affectedShots.join("、") || preview.shot_no || "—")}</p><details class="ui00-script-impact-details"><summary>更新范围与来源</summary>${rerun}<p>${escapeHtml(response.source_impact || preview.source_change || "来源不变")}</p></details>${(preview.warnings || []).map((item) => `<p>${escapeHtml(item)}</p>`).join("")}${formalScriptModelEvidence(response)}<div class="ui00-inline-actions"><button class="primary-button compact" type="button" data-confirm-script-revision="1">${confirmLabel}</button>${formalScriptNextHref(response)}</div>`;
  } else if (responseType === "no_changes") {
    proposal.innerHTML = `<strong>当前无需修改</strong><p>${escapeHtml(response.summary || "当前脚本已符合这次要求。")}</p>`;
  } else if (responseType === "source_match_required") {
    proposal.innerHTML = `<strong>${escapeHtml(response.title || "需要先完成来源匹配")}</strong><p>${escapeHtml(response.summary || "当前没有满足来源约束的可执行镜头。")}</p>${formalScriptModelEvidence(response)}${formalScriptNextHref(response, "前往来源匹配")}`;
  } else if (responseType === "replan_required") {
    proposal.innerHTML = `<strong>${escapeHtml(response.title || "需要返回规划")}</strong><p>${escapeHtml(response.summary || "本次修改会改变规划边界，不能在脚本修订中直接执行。")}</p>${formalScriptModelEvidence(response)}${formalScriptNextHref(response, "返回规划")}`;
  } else if (responseType === "needs_clarification") {
    proposal.innerHTML = `<strong>${escapeHtml(response.summary || "需要补充修改范围")}</strong><p>${escapeHtml(response.next_steps?.[0] || "请补充明确要求。")}</p>${(response.choices || []).map((choice) => {
      const instruction = choice.instruction || choice.value || "";
      return `<button class="secondary-button compact" type="button" data-script-revision-instruction="${escapeHtml(instruction)}"${instruction ? "" : " disabled"}>${escapeHtml(choice.label)}</button>`;
    }).join(" ")}${formalScriptNextHref(response)}`;
  } else {
    proposal.innerHTML = `<strong>${escapeHtml(response.title || "当前无法修改")}</strong><p>${escapeHtml(response.summary || "请检查能力状态。")}</p>${formalScriptModelEvidence(response)}${formalScriptNextHref(response)}`;
  }
  if (responseType === "action_proposal") void loadFormalScriptImageChanges(response, proposal);
  elements.formalScriptInstructionSubmit.hidden = ["scope_required", "action_proposal"].includes(responseType);
  syncCreativePrimaryAction();
}

async function updateCreativeProposalSelection(input) {
  const pending = state.formalScriptRevisionProposal;
  const projectId = formalRoute().projectId;
  if (!pending?.task_id || !pending.response?.draft_only || state.formalScriptBusy) return;
  const retained = [...elements.formalScriptRevisionProposal.querySelectorAll("[data-creative-retain-shot]:checked")].map(node => node.dataset.creativeRetainShot);
  const controls = [...elements.formalScriptRevisionProposal.querySelectorAll("input, button")];
  controls.forEach(node => { node.disabled = true; });
  state.formalScriptBusy = true;
  try {
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/script/revisions/${encodeURIComponent(pending.task_id)}/selection?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, { method: "POST", body: JSON.stringify({
      expected_project_version: state.formalScript.project_version, expected_revision: state.formalScript.revision,
      retained_shot_ids: retained, idempotency_key: formalRequestKey("revision-selection"),
    }) });
    if (formalRoute().projectId !== projectId || state.formalScriptRevisionProposal !== pending) return;
    renderFormalScriptRevisionProposal(result, state.formalScriptProposalInstruction);
    showToast(retained.length ? `已保留 ${retained.length} 镜建议，确认后应用。` : "本次建议已全部取消。");
  } catch (error) {
    if (input.isConnected) input.checked = true;
    showToast(formalError(error));
  } finally { state.formalScriptBusy = false; controls.filter(node => node.isConnected).forEach(node => { node.disabled = false; }); syncCreativePrimaryAction(); }
}

async function requestFormalScriptProposal({ analysisTaskId = "", selectedShotIds = [], instruction = "" } = {}) {
  const projectId = state.formalScriptProjectId || formalRoute().projectId;
  const epoch = state.formalScriptEpoch;
  const request = ++state.formalScriptProposalRequest;
  const originalInstruction = instruction || elements.formalScriptInstruction.value.trim();
  const active = () => state.formalScriptProposalRequest === request && state.formalScriptEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "script";
  if (!(await flushFormalScriptChanges()) || !active()) return;
  const proposal = elements.formalScriptRevisionProposal;
  proposal.hidden = false;
  elements.formalScriptSaveProposal.hidden = true;
  proposal.textContent = "正在预览修改…";
  elements.formalScriptInstructionSubmit.disabled = true;
  elements.formalScriptScopeContinue.disabled = true;
  try {
    const body = {
      expected_revision: Number(state.formalScript.revision || 0),
      expected_project_version: Number(state.formalScript.project_version || 0),
      instruction: originalInstruction,
      idempotency_key: formalRequestKey("script-revision-proposal"),
      entrypoint: "assistant",
      feedback_kind: state.formalScriptFeedbackKind || "copy",
    };
    const selected = selectedShotIds.length ? selectedShotIds : creativeAllowedShotIds(creativeState().assistantScope, false);
    const protectedIds = new Set((state.formalScript?.shots || []).filter(shot => shot.assistant_locked).map(shot => shot.shot_id));
    body.selected_shot_ids = selected.filter(id => !protectedIds.has(id));
    if (!body.selected_shot_ids.length) {
      proposal.innerHTML = '<strong>请选择本次可修改的镜头</strong><p>受保护镜头不会参与助手修改。</p>';
      return;
    }
    if (analysisTaskId) body.analysis_task_id = analysisTaskId;
    const createImage = /(?:生成|生|画|做).{0,8}(?:一张|图片|参考图)|保留.{0,25}(?:换|改).{0,12}(?:背景|场景)|改图|编辑.{0,8}(?:图片|原图)/.test(originalInstruction);
    if (createImage) {
      if (body.selected_shot_ids.length !== 1) { proposal.textContent = "请先选定一个要生成新图的镜头，每次生成一张。"; return; }
      await openCurrentImageCreation("formal_shot", body.selected_shot_ids[0], originalInstruction);
      proposal.textContent = "已打开图片创作窗口，请核对要求与参考图后生成。";
      return;
    }
    const explicitImages = /(?:配|换|替换|找|检索|搜索).{0,20}(?:图|素材)|(?:图片|配图).{0,12}(?:换|替换|检索|搜索|推荐)/.test(originalInstruction);
    const explicitText = /画面描述|字幕|旁白|配音|段落名称|时长|台词/.test(originalInstruction) && !explicitImages;
    if (explicitImages || document.querySelector('#creative-assistant-kind').value === 'images' && !explicitText) {
      if (!await flushCreativeProgress({ manual: true }) || !active()) return;
      const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/assistant?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, { method: "POST", body: JSON.stringify({
        expected_project_version: body.expected_project_version, instruction: originalInstruction,
        selected_shot_ids: selected, idempotency_key: formalRequestKey("image-batch-proposal"),
      }) });
      if (!active()) return;
      creativeState().imageFlow = result;
      proposal.innerHTML = imageBatchProposalHtml(result.response, result.task.task_id);
      await loadImageBatchPreviews(proposal);
      return;
    }
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/script/draft-proposals?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, { method: "POST", body: JSON.stringify(body) });
    if (!active()) return;
    renderFormalScriptRevisionProposal(result, originalInstruction);
  } catch (error) {
    if (active()) proposal.innerHTML = `<strong>修改未完成</strong><p>${escapeHtml(formalError(error))}</p>`;
  } finally {
    if (active()) {
      elements.formalScriptInstructionSubmit.disabled = false;
      if (!elements.formalScriptScope.hidden) elements.formalScriptScopeContinue.disabled = !state.formalScriptScopeTask;
    }
  }
}

async function applyFormalScriptInstruction(event) {
  event.preventDefault();
  if (!elements.formalScriptInstructionForm.reportValidity() || !state.formalScript) return;
  await requestFormalScriptProposal();
}

async function continueFormalScriptScope() {
  if (!state.formalScriptScopeTask) return;
  const selectedShotIds = Array.from(elements.formalScriptScopeOptions.querySelectorAll("[data-script-scope-shot]:checked")).map((input) => input.value);
  if (!selectedShotIds.length) {
    showToast("请至少选择一个建议镜头。");
    return;
  }
  await requestFormalScriptProposal({
    analysisTaskId: state.formalScriptScopeTask.analysis_task_id,
    selectedShotIds,
    instruction: state.formalScriptScopeTask.instruction,
  });
}

async function confirmFormalScriptRevision() {
  const pending = state.formalScriptRevisionProposal;
  if (!pending?.task_id || !pending?.response?.proposal_digest || pending.response.workbench_parent_task_id
      || pending.task?.workbench_parent_task_id || !state.formalScript || state.formalScriptBusy) return;
  const projectId = formalRoute().projectId;
  const surface = formalSurface();
  let epoch = state.formalScriptEpoch;
  const active = () => state.formalScriptEpoch === epoch && formalRoute().projectId === projectId && formalSurface() === surface;
  const draftOnly = Boolean(pending.response.draft_only);
  const instruction = state.formalScriptProposalInstruction;
  const panel = draftOnly ? elements.formalScriptRevisionProposal : elements.formalScriptSaveProposal;
  if (!(await flushFormalScriptChanges()) || !active() || state.formalScriptRevisionProposal !== pending) return;
  state.formalScriptBusy = true;
  renderFormalScript(state.formalScript);
  panel.textContent = draftOnly ? "正在应用修改…" : "正在保存新版本…";
  const recoveryKey = `mplus-version-confirm:${state.agentWorkspaceId}:${projectId}`;
  if (!draftOnly) sessionStorage.setItem(recoveryKey, JSON.stringify(pending));
  try {
    let result = null;
    if (pending.result_unknown) {
      const receipt = await api(`/api/agent/tasks/${encodeURIComponent(pending.task_id)}`);
      if (receipt?.task?.status === "completed") result = receipt;
    }
    if (!result) result = await api(`/api/agent/tasks/${encodeURIComponent(pending.task_id)}/confirm`, { method: "POST", body: JSON.stringify({ proposal_digest: pending.response.proposal_digest }) });
    if (!active()) return;
    requireCompletedAgentAction(result);
    if (result?.task?.status && result.task.status !== "completed") throw new Error("保存结果仍在核实中");
    if (!draftOnly) sessionStorage.removeItem(recoveryKey);
    state.formalScriptBusy = false;
    const loading = loadFormalScript(projectId);
    epoch = state.formalScriptEpoch;
    await loading;
    if (!active()) return;
    await refreshCreativeOverview(projectId);
    if (!active()) return;
    creativeState().imageKey = "";
    syncCreativeVisibility();
    panel.hidden = false;
    if (elements.formalScriptInstruction.value.trim() === instruction.trim()) state.uiInputBaselines.delete(elements.formalScriptInstruction);
    if (draftOnly) {
      if (!elements.formalScriptSaveReason.value.trim()) elements.formalScriptSaveReason.value = instruction.slice(0, 500);
      panel.innerHTML = '<strong>修改已应用到草稿</strong><p>核对后，点击“保存为新版本”。</p><button class="secondary-button compact" type="button" data-script-next-save="1">前往保存</button>';
    } else {
      panel.innerHTML = `<strong>文字与配图已保存为一个新版本</strong><a class="primary-button compact" href="${formalPath("review", projectId)}" data-script-next-href="1">进入人工审核</a>`;
      void loadFormalShotImages(projectId);
      if (creativeState().continueImagesAfterSave) { creativeState().continueImagesAfterSave = false; await changeCreativePanel("images"); }
    }
  } catch (error) {
    if (!active()) return;
    state.formalScriptBusy = false;
    renderFormalScript(state.formalScript);
    panel.hidden = false;
    const known = error.status >= 400 && error.status < 500 && error.status !== 408 && error?.payload?.detail?.code !== "ACTION_RESULT_UNKNOWN";
    if (!draftOnly && !known) {
      pending.result_unknown = true;
      sessionStorage.setItem(recoveryKey, JSON.stringify(pending));
      state.formalScriptRevisionProposal = pending;
      panel.innerHTML = '<strong>保存结果待核实</strong><p>原确认请求已保留，请读取原结果后继续。</p><button type="button" data-confirm-script-revision="1">核实保存结果</button>';
    } else {
      if (!draftOnly) sessionStorage.removeItem(recoveryKey);
      invalidateFormalScriptProposal();
      panel.hidden = false;
      panel.innerHTML = `<strong>修改未完成</strong><p>${escapeHtml(formalError(error))}</p>`;
    }
  } finally {
    if (state.formalScriptEpoch === epoch && formalRoute().projectId === projectId) state.formalScriptBusy = false;
    if (active()) syncFormalScriptSaveControls(state.formalScript);
  }
}

function requireCompletedAgentAction(result) {
  const response = result?.response || {};
  const failed = result?.task?.status === "failed" || response.type === "action_failed";
  if (!failed && result?.task?.status !== "blocked" && response.type !== "action_blocked") return result;
  throw {
    status: ["ADMIN_RECOVERY_REQUIRED", "RECOVERY_RECONCILIATION_REQUIRED", "INTERNAL_ERROR"].includes(response.error_code) ? 503 : 409,
    payload: {
      detail: {
        code: response.error_code || result?.task?.error_code || (failed ? "FORMAL_EXECUTION_FAILED" : "PROJECT_STATE_CHANGED"),
        message: response.summary || "项目状态已变化，请刷新后重试。",
      },
    },
  };
}

async function refreshCreativeAfterScriptHistory(current, projectId) {
  if (creativeState() !== current || formalRoute().projectId !== projectId) return;
  const navigationRequest = current.navigationRequest;
  current.imageKey = "";
  await refreshCreativeOverview(projectId);
  const active = () => creativeState() === current && current.navigationRequest === navigationRequest
    && formalRoute().page === "script" && formalRoute().projectId === projectId;
  if (!active()) return;
  const route = formalRoute();
  const key = `${current.versionId}:${state.formalScript?.project_version || ""}`;
  if (!route.aux && route.panel === "images" && !state.formalScript?.can_save_version && !formalScriptHasPendingWrites()
      && !state.formalImageDirty && (current.imageKey !== key || !state.formalImageWorkspace)) {
    await loadFormalShotImages(projectId);
    if (!active()) return;
    current.imageKey = key;
  }
  syncCreativeVisibility();
}

async function formalScriptHistory(action) {
  if (!state.formalScript) return;
  if (!(await flushFormalScriptChanges())) return null;
  const current = creativeState();
  const projectId = current.projectId;
  const model = await runFormalScriptAction(action, {
    expected_revision: Number(state.formalScript.revision || 0),
    expected_project_version: Number(state.formalScript.project_version || 0),
    idempotency_key: formalRequestKey(`script-${action}`),
  });
  if (model && action !== "discard") await refreshCreativeAfterScriptHistory(current, projectId);
  return model;
}

async function discardFormalScriptChanges() {
  const projectId = formalRoute().projectId;
  if (!projectId || state.formalScriptDiscarding) return;
  const surface = formalSurface();
  let epoch = state.formalScriptEpoch;
  const active = () => state.formalScriptEpoch === epoch && formalRoute().projectId === projectId && formalSurface() === surface;
  const storageKey = `mplus-discard-all:${state.agentWorkspaceId}:${projectId}`;
  let pending = JSON.parse(sessionStorage.getItem(storageKey) || "null");
  state.formalScriptDiscarding=true;
  try {
    if (!pending) {
      captureFormalImageInputs();
      const queue = creativeProgressQueue();
      window.clearTimeout(state.formalScriptSaveTimer);
      if (queue) window.clearTimeout(queue.timer);
      await Promise.all([state.formalScriptSavePromise, queue?.promise].filter(Boolean));
      if (!active()) return;
      const [model, progress] = await Promise.all([
        api(formalScriptPath(projectId)),
        api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/progress?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`),
      ]);
      if (!active()) return;
      const count = creativeChangedImageShots().length;
      if (!window.confirm(`放弃全部草稿修改？\n文字：${model.can_save_version || formalScriptHasPendingWrites() ? "全部待保存文字改动" : "没有文字改动"}\n配图：全稿配图草稿（当前可见 ${count} 镜修改）\n恢复已保存版本，保留素材与历史版本。`)) return;
      pending = {projectId, phase:"text",
        text:{expected_project_version:model.project_version, expected_revision:model.revision, idempotency_key:formalRequestKey("discard-text")},
        images:{base_version_id:model.base_version_id, expected_project_version:model.project_version,
          expected_revision:progress.revision || 0, expected_script_revision:model.revision,
          selections:[],idempotency_key:formalRequestKey("discard-images")}};
      sessionStorage.setItem(storageKey,JSON.stringify(pending));
    }
    if (pending.phase === "text") {
      const current = await api(formalScriptPath(projectId));
      if (current.base_version_id !== pending.images.base_version_id) throw new Error("当前版本已变化，请重新核对放弃范围。");
      let model=current;
      if (current.can_save_version) {
        model=await api(formalScriptPath(projectId,"discard"),{method:"POST",body:JSON.stringify(pending.text)});
      }
      pending.images.expected_script_revision=model.revision;
      pending.phase="images";
      sessionStorage.setItem(storageKey,JSON.stringify(pending));
    }
    await api(`/api/formal/projects/${encodeURIComponent(projectId)}/image-workspace/progress/discard?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`,{method:"POST",body:JSON.stringify(pending.images)});
    sessionStorage.removeItem(storageKey);
    sessionStorage.removeItem(formalImageInputKey(projectId,pending.images.base_version_id));
    if (!active()) return;
    const queue=creativeProgressQueue();
    if(queue){window.clearTimeout(queue.timer);queue.desired.clear();queue.baseline.clear();queue.model=null;queue.retry=null;}
    state.formalScriptDirtyCells.clear();state.formalScriptSaveRetryPending=null;
    state.formalScriptRejectedCells.clear();state.formalScriptSaveConflict=null;
    const loading = loadFormalScript(projectId);
    epoch = state.formalScriptEpoch;
    await loading;
    if (!active()) return;
    await loadFormalShotImages(projectId);
    if (!active()) return;
    await refreshCreativeOverview(projectId);
    if (!active()) return;
    showToast("全部草稿修改已放弃，已恢复保存版本。");
  } catch(error) {
    const rejected = error.status >= 400 && error.status < 500 && error.status !== 408;
    if (rejected) sessionStorage.removeItem(storageKey);
    if(active()) {
      if (rejected && pending) {
        const loading = loadFormalScript(projectId);
        epoch = state.formalScriptEpoch;
        await loading;
        if (!active()) return;
      }
      setFormalScriptStatus("error", rejected
        ? `${pending?.phase === "images" ? "文字恢复步骤已完成，配图修改仍保留。" : "当前修改已保留。"}请重新核对放弃范围。${formalError(error)}`
        : `放弃操作尚未完成；再次点击可继续原请求。${formalError(error)}`);
    }
  } finally {
    if (state.formalScriptEpoch === epoch && formalRoute().projectId === projectId) state.formalScriptDiscarding=false;
    if (active()) syncFormalScriptSaveControls(state.formalScript);
  }
}

async function saveFormalScriptVersion(event) {
  event.preventDefault();
  captureFormalImageInputs();
  if (!await flushCreativeProgress({ manual: true })) return;
  if (!elements.formalScriptSaveForm.reportValidity() || !state.formalScript) return;
  const projectId = state.formalScriptProjectId || formalRoute().projectId;
  const recover = sessionStorage.getItem(`mplus-version-confirm:${state.agentWorkspaceId}:${projectId}`);
  if (recover) {
    const pending = JSON.parse(recover); pending.result_unknown = true;
    renderFormalScriptRevisionProposal(pending);
    elements.formalScriptSaveProposal.insertAdjacentHTML("afterbegin", "<p>上次保存结果待核实。点击确认将回读原请求，不创建第二个提案。</p>");
    return;
  }
  if (!elements.formalScriptSaveReason.value.trim()) {
    elements.formalScriptSaveProposal.hidden = false;
    elements.formalScriptSaveProposal.textContent = "请填写本次修改说明，不能只填空格。已保存的进度仍保留，填写后可继续保存版本。";
    elements.formalScriptSaveReason.focus({ preventScroll: true });
    return;
  }
  const epoch = state.formalScriptEpoch;
  const request = ++state.formalScriptProposalRequest;
  const imageRevision = state.formalImageInputRevision;
  const active = () => state.formalScriptProposalRequest === request && state.formalImageInputRevision === imageRevision && state.formalScriptEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "script";
  if (!(await flushFormalScriptChanges()) || !active()) return;
  elements.formalScriptSaveProposal.hidden = false;
  elements.formalScriptRevisionProposal.hidden = true;
  elements.formalScriptSaveProposal.textContent = "正在检查全稿配图并汇总文字与图片改动…";
  elements.formalScriptSave.disabled = true;
  try {
    const result = await api(formalScriptPath(projectId, "save"), { method: "POST", body: JSON.stringify({
      expected_revision: Number(state.formalScript.revision || 0),
      expected_project_version: Number(state.formalScript.project_version || 0),
      ...(creativeProgressQueue()?.model ? {expected_progress_revision: Number(creativeProgressQueue().model.revision)} : {}),
      reason: elements.formalScriptSaveReason.value.trim(),
      idempotency_key: formalRequestKey("script-version"),
    }) });
    if (active()) renderFormalScriptRevisionProposal(result);
  } catch (error) {
    if (active()) {
      const issues = error?.payload?.detail?.issues || [];
      elements.formalScriptSaveProposal.innerHTML = `<strong>${issues.length ? `进度已保存，尚有 ${issues.length} 镜待处理` : "版本未保存"}</strong><p>${escapeHtml(formalError(error))}</p>${issues.map(item => `<p><a data-script-next-href href="${escapeHtml(formalPath("shot-images", projectId, {focus_shot: item.shot_no, shot_id: item.shot_id}))}">第 ${escapeHtml(item.shot_no)} 镜</a>：${escapeHtml(item.reason)}</p>`).join("")}`;
    }
  } finally {
    if (state.formalScriptEpoch === epoch && formalRoute().projectId === projectId && formalRoute().page === "script") {
      if (state.formalScriptProposalRequest === request && state.formalImageInputRevision !== imageRevision) {
        elements.formalScriptSaveProposal.textContent = "配图已变化，请重新预览版本影响。";
      }
      syncFormalScriptSaveControls(state.formalScript);
    }
  }
}

async function downloadFormalScriptReviewWorkbook(versionNo = null, sourceButton = null) {
  if (!state.formalScript || !["ready", "editing"].includes(state.formalScript.state)) return;
  const current = (state.formalScript.versions || []).find((version) => versionNo == null ? version.is_current : version.version_no === versionNo);
  if (!current) return;
  const button = sourceButton || elements.formalScriptReviewWorkbook;
  return downloadSavedReviewWorkbook(formalRoute().projectId, current.version_no, button);
}

async function downloadSavedReviewWorkbook(projectId, versionNo, button) {
  if (!projectId || !versionNo) return;
  button.disabled = true;
  try {
    const path = `/api/formal/projects/${encodeURIComponent(projectId)}/script/versions/${encodeURIComponent(versionNo)}/review-workbook?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
    const headers = new Headers();
    if (state.token) headers.set("X-MPlus-UI-Token", state.token);
    if (state.agentAuth) headers.set("X-MPlus-Agent-Auth", state.agentAuth);
    const response = await fetch(path, { headers, cache: "no-store" });
    if (!response.ok) {
      const payload = (response.headers.get("content-type") || "").includes("application/json")
        ? await response.json() : null;
      const error = new Error("复核稿下载失败");
      error.payload = payload;
      throw error;
    }
    await downloadResponse(response, "脚本复核稿.xlsx");
  } catch (error) {
    showToast(formalError(error));
  } finally {
    button.disabled = false;
  }
}

function resetFormalPlan() {
  state.formalPlanRequest += 1;
  state.formalPlanProjectId = "";
  state.formalPlan = null;
  state.formalPlanBusy = false;
  state.formalPlanTextOnly = false;
  state.formalPlanZeroImage = false;
  state.pendingPlanConflictAction = null;
  elements.formalPlanBriefBlock.hidden = true;
  elements.formalPlanStandardBlock.hidden = true;
  elements.formalPlanImageBlock.hidden = true;
  delete elements.formalPlanImageBlock.dataset.creativeReferenceCopy;
  elements.formalPlanImageBlockTitle.textContent = "请先完成项目图片理解";
  elements.formalPlanAction.hidden = true;
  elements.formalPlanPreview.hidden = true;
  elements.formalPlanConfirmation.hidden = true;
  elements.formalPlanResult.hidden = true;
  elements.formalPlanConfirmCheck.checked = false;
  elements.formalPlanConfirm.disabled = true;
  elements.formalPlanCreate.disabled = false;
  elements.formalPlanCreate.textContent = "生成规划";
  elements.formalPlanCapacity.innerHTML = "";
  elements.formalPlanDecisionCounts.innerHTML = "";
  elements.formalPlanConflicts.hidden = true;
  elements.formalPlanConflictList.innerHTML = "";
  elements.formalPlanConflictConfirmation.hidden = true;
  elements.formalPlanConflictCheck.checked = false;
  elements.formalPlanConflictExecute.disabled = true;
}

function planPositionLabel(rule = {}) {
  const type = String(rule.type || "none");
  if (type === "opening") return "第 1 镜／开头";
  if (type === "closing") return "最后一镜／结尾";
  if (type === "fixed_shot") return `第 ${rule.shot_no || "—"} 镜`;
  if (type === "before") return `在 ${rule.anchor_text || rule.anchor_event_id || "指定事件"} 之前`;
  if (type === "after") return `在 ${rule.anchor_text || rule.anchor_event_id || "指定事件"} 之后`;
  return "无固定位置";
}

function planTraceTable(traceability = []) {
  if (!traceability.length) return '<div class="workbench-empty">暂无跨文件追踪记录。</div>';
  return `<div class="ui00-plan-coverage-scroll"><table class="ui00-plan-trace-table"><colgroup><col style="width:55%"><col style="width:25%"><col style="width:20%"></colgroup><thead><tr><th>资料要求</th><th>对应镜头</th><th>状态</th></tr></thead><tbody>${traceability.map((item) => {
    const sources = (item.template_source || []).map(planSourceLabel).filter(Boolean).join("；") || "未使用模板事实";
    const outputCount = (item.output_evidence || []).length;
    const fragments = (item.output_evidence || []).flatMap(row => (row.content_evidence?.fragments || []).map(fragment =>
      `<li>第 ${escapeHtml(row.shot_no)} 镜 · ${escapeHtml(fragment.field)}：${escapeHtml(fragment.text)}${fragment.negated_or_uncertain ? "（含否定或待确认表述）" : fragment.generic_restatement ? "（可能仅复述要求）" : ""}${row.content_evidence?.missing_requirement_parts?.length ? `（尚未定位：${escapeHtml(row.content_evidence.missing_requirement_parts.join("、"))}）` : ""}</li>`));
    const currentText = (item.output_evidence || []).map(row => `<li>第 ${escapeHtml(row.shot_no)} 镜 · 当前画面：${escapeHtml(row.visual_description || "未填写")}<br>声音：${escapeHtml(row.sound || "未填写")}</li>`);
    const literalEvidence = fragments.length
      ? `<p>以下为原稿文字提及，具体动作与要求是否完成仍需人工核对。</p><ul>${fragments.join("")}</ul>`
      : currentText.length ? `<p>未定位原文提及；请核对下列当前内容是否以其他表述落实要求。</p><ul>${currentText.join("")}</ul>` : "";
    const reviewCount = (item.review_findings || []).length;
    const status = item.classification === "production_requirement" && item.final_status === "covered" ? "制作要求已记录" : ({ covered: "已分配镜头", output_verified: "成稿文字有依据", pending_review: "成稿待复核", optional: "可选", pending: "待处理", blocked: "需修改", passed: "已通过", pass: "已通过" })[item.final_status] || "待核对";
    return `<tr><td data-label="资料要求"><strong>${escapeHtml(item.brief_text || "未提供")}</strong><details><summary>查看依据</summary><p>${escapeHtml(planPositionLabel(item.position_rule || {}))}</p><p>${escapeHtml(sources)}</p><small>${outputCount ? `已关联 ${outputCount} 条镜头记录` : "等待生成"}${reviewCount ? `／${reviewCount} 条待复核` : ""}</small>${literalEvidence}</details></td><td data-label="对应镜头">${escapeHtml((item.plan_shot_nos || []).join("、") || "—")}</td><td data-label="状态"><span data-coverage-status="${escapeHtml(item.final_status || "unknown")}">${escapeHtml(status)}</span></td></tr>`;
  }).join("")}</tbody></table></div>`;
}

const planDecisionCountDefinitions = [
  ["requirements_uncovered", "需求未覆盖", "coverage"],
  ["hard_template_downgrades", "模板需调整", "shots"],
  ["unresolved_conflicts", "未解决冲突", "conflicts"],
  ["unexplained_overrides", "取舍待说明", "shots"],
];

function planDecisionCountsMarkup(counts = {}, context = "plan") {
  return planDecisionCountDefinitions.map(([key, label, target]) => {
    const value = Math.max(0, Number(counts[key] || 0));
    const destination = context === "review" ? "review-trace" : target;
    return `<button type="button" data-plan-decision-target="${destination}" data-count-key="${key}" ${value ? "" : "disabled"}><span>${escapeHtml(label)}</span><strong>${value}</strong><small>${value ? "点击定位" : "已清零"}</small></button>`;
  }).join("");
}

function planHasDecisionGate(plan = {}) {
  if (!plan) return false;
  const counts = plan.decision_counts || {};
  const audit = plan.audit || {};
  return Boolean(
    plan.needs_clarification
    || audit.status === "needs_clarification"
    || audit.status === "blocked"
    || Number(counts.unresolved_conflicts || 0)
    || Number(counts.unexplained_overrides || 0)
  );
}

function planDecisionValue(value) {
  if (value === null || value === undefined || value === "") return "待确定";
  if (Array.isArray(value)) return value.map(planDecisionValue).join("、");
  if (typeof value === "object") {
    const label = value.label || value.summary || value.description || value.text;
    if (label) return String(label);
    const labels = { shot_no: "镜头", shot_count: "镜数", duration_seconds: "时长", structure: "结构", visual: "画面", sound: "声音", start: "起始", end: "结束", type: "位置规则", value: "内容" };
    const positions = { none: "无位置要求", fixed_shot: "固定镜头", first: "开场", last: "结尾", opening: "开场", ending: "结尾", before: "在前", after: "在后" };
    return Object.entries(value).filter(([, item]) => item !== null && item !== undefined && item !== "").map(([key, item]) => {
      const text = key === "type" ? positions[item] || "指定位置" : planDecisionValue(item);
      return labels[key] ? `${labels[key]}：${text}` : text;
    }).join(" · ") || "待确定";
  }
  return String(value);
}

function planFieldDecisions(decisions = []) {
  if (!decisions.length) return '<p class="workbench-empty">旧版规划未记录逐字段决策。</p>';
  return `<div class="ui00-field-decisions">${decisions.map((decision) => {
    const candidates = (decision.candidates || []).map((candidate) => {
      const source = candidate.source === "brief" ? "Brief" : candidate.source === "template_rule" ? "模板规则" : candidate.source || "其他来源";
      return `<li><strong>${escapeHtml(source)}</strong><span>${escapeHtml(planDecisionValue(candidate.value))}</span></li>`;
    }).join("");
    const status = ({ applied: "已采用", downgraded: "按需求调整", selected: "已确定", preserved: "沿用", overridden: "已调整", conflict: "待确认" })[decision.decision_status] || "待核对";
    const field = ({structure:"结构", visual:"画面", sound:"声音", duration_seconds:"时长", shot_type:"镜头类型", shot_size:"景别", camera_movement:"运镜", position_rule:"位置", primary_event:"内容", text_density:"文字密度"})[decision.field] || "其他要求";
    const value = decision.final_value && typeof decision.final_value === "object" ? `<details><summary>查看具体要求</summary><p>${escapeHtml(planDecisionValue(decision.final_value))}</p></details>` : `<p>${escapeHtml(planDecisionValue(decision.final_value))}</p>`;
    return `<article data-decision-status="${escapeHtml(decision.decision_status || "unknown")}"><header><strong>${escapeHtml(field)}</strong><span>${escapeHtml(status)}</span></header>${value}${candidates ? `<details><summary>比较依据</summary><ul>${candidates}</ul></details>` : ""}</article>`;
  }).join("")}</div>`;
}

function renderFormalPlanTemplateSummary(plan) {
  const summary = plan.template_summary;
  if (!summary) {
    elements.formalPlanTemplateSummary.innerHTML = '<p>此规划未记录模板概览，可在镜头依据中核对来源。</p>';
    return;
  }
  const original = summary.original_segments || [], current = summary.current_segments || [];
  const originalCount = original.reduce((total, item) => total + Number(item.shot_count || 0), 0);
  const currentCount = current.reduce((total, item) => total + Number(item.shot_count || 0), 0) || Number(plan.shot_count || 0);
  const segments = items => `<ol class="ui00-plan-segments">${items.map(item => `<li><strong>${escapeHtml(item.structure)}</strong><span>${escapeHtml(item.shot_count)} 镜</span></li>`).join("")}</ol>`;
  const adjustments = Array.isArray(summary.adjustments) ? summary.adjustments : [];
  const adjustmentList = adjustments.length ? `<section class="creative-plan-adjustments"><h4>本次具体调整</h4><ul>${adjustments.map(item => `<li><strong>${escapeHtml(item.summary || "按当前需求调整")}</strong>${item.before !== undefined || item.after !== undefined ? `<span>${escapeHtml(planDecisionValue(item.before))} → ${escapeHtml(planDecisionValue(item.after))}</span>` : ""}</li>`).join("")}</ul></section>` : `<p>${escapeHtml(summary.structure_note || "当前结构沿用模板，未记录具体调整。")}</p>`;
  elements.formalPlanTemplateSummary.innerHTML = `<header><div><span>本次规划采用</span><h3>${escapeHtml(summary.label || "模板")} · 版本 ${escapeHtml(summary.package_version || "—")}</h3></div><b>${summary.selection_mode === "explicit" ? "项目已选模板" : "类别默认模板"}</b></header><div class="ui00-plan-structure-comparison"><section><h4>原模板 · ${originalCount} 镜</h4>${segments(original)}</section><section><h4>当前规划 · ${currentCount} 镜</h4>${segments(current)}</section></div><p class="ui00-plan-adjustment">按 Brief 调整 ${Number(summary.structure_change_count || 0)} 处 · 位置要求落实 ${Number(summary.position_requirement_count || 0)} 项${currentCount !== originalCount ? ` · 镜数 ${originalCount} → ${currentCount}` : ""}</p>${adjustmentList}${formalTemplateSourcesHtml(summary.sources)}`;
}

function renderFormalPlanConflicts(conflicts = []) {
  const unresolved = conflicts.filter((item) => item.status === "unresolved");
  elements.formalPlanConflicts.hidden = !unresolved.length;
  elements.formalPlanConflictConfirmation.hidden = true;
  elements.formalPlanConflictCheck.checked = false;
  elements.formalPlanConflictExecute.disabled = true;
  state.pendingPlanConflictAction = null;
  if (!unresolved.length) {
    elements.formalPlanConflictList.innerHTML = "";
    return;
  }
  elements.formalPlanConflictList.innerHTML = unresolved.map((item, index) => item.type === "creative_brief_flow_conflict"
    ? `<article class="ui00-plan-conflict-card"><strong>初稿与已确认流程需要调整</strong><p>${escapeHtml(item.message)}</p><a href="${escapeHtml(formalPath("script", formalRoute().projectId))}">返回初稿</a><a href="${escapeHtml(formalPath("brief", formalRoute().projectId))}">核对资料流程</a></article>`
    : `<article class="ui00-plan-conflict-card" data-conflict-id="${escapeHtml(item.conflict_id || "")}"><header><span>镜头 ${escapeHtml(item.shot_no || "—")}</span><strong>${escapeHtml(item.field || "structure")}</strong></header><div class="ui00-plan-conflict-values"><div><span>资料要求</span><p>${escapeHtml(item.brief_value || "未提供")}</p></div><div><span>模板建议</span><p>${escapeHtml(item.template_value || "未提供")}</p><small>${escapeHtml(item.rule_id || "规则 ID 未提供")}</small></div></div><fieldset><legend>项目实际采用的内容</legend><label><input type="radio" name="plan-conflict-${index}" value="brief_wins" />采用资料要求</label><label><input type="radio" name="plan-conflict-${index}" value="template_wins" />采用模板建议</label></fieldset></article>`).join("");
  elements.formalPlanConflictPrepare.hidden = unresolved.some(item => item.type === "creative_brief_flow_conflict");
}

function renderFormalPlanPreview(plan) {
  const samePlan = state.formalPlan?.plan_id && state.formalPlan.plan_id === plan.plan_id;
  const expanded = samePlan ? new Set([...elements.formalPlanPreview.querySelectorAll("details[open]")].map(item => item.id || item.querySelector("summary")?.textContent)) : new Set();
  const choices = samePlan ? new Map([...elements.formalPlanConflictList.querySelectorAll("[data-conflict-id]")].map(card => [card.dataset.conflictId, card.querySelector("input:checked")?.value])) : new Map();
  elements.creativeCurrentStructure.hidden = true;
  state.formalPlan = plan;
  renderFormalPlanTemplateSummary(plan);
  if (plan.creative_conversion?.mappings?.length) {
    const conversion = plan.creative_conversion;
    elements.formalPlanTemplateSummary.insertAdjacentHTML('beforeend', `<section class="creative-conversion-map"><h3>初稿如何对应正式镜头</h3><p>保留 ${conversion.mapped_count} 段表达与顺序，补充 ${conversion.added_shot_count} 镜；${conversion.protected_count || 0} 段保护范围随稿保留。</p><table><thead><tr><th>初稿段落</th><th>正式镜头</th><th>时长变化</th><th>内容</th></tr></thead><tbody>${conversion.mappings.map(item => `<tr><td>${escapeHtml(item.section)}</td><td>第 ${escapeHtml(item.shot_no)} 镜</td><td>${escapeHtml(item.before_duration)} → ${escapeHtml(item.after_duration_seconds)} 秒</td><td>画面与声音原文保留${item.assistant_protected ? ' · 助手保留' : ''}</td></tr>`).join('')}</tbody></table></section>`);
  }
  const events = Array.isArray(plan.events) ? plan.events : [];
  const shots = Array.isArray(plan.shots) ? plan.shots : [];
  const requirements = Array.isArray(plan.requirements) ? plan.requirements : [];
  const coverage = Array.isArray(plan.coverage) ? plan.coverage : [];
  const traceability = Array.isArray(plan.traceability) ? plan.traceability : [];
  const capacity = plan.capacity_report || {};
  const decisionCounts = plan.decision_counts || {};
  const conflicts = Array.isArray(plan.conflicts) ? plan.conflicts : [];
  const needsAttention = planHasDecisionGate(plan) || capacity.status === "needs_clarification" || Object.values(decisionCounts).some(value => Number(value) > 0);
  elements.formalPlanCapacityDetails.open = needsAttention;
  elements.formalPlanShotsDetails.open = needsAttention;
  elements.formalPlanShotsSummary.textContent = `查看 ${Number(plan.shot_count || shots.length)} 镜安排`;
  const requirementsById = Object.fromEntries(requirements.map((item) => [item.requirement_id, item]));
  elements.formalPlanSummary.textContent = `${Number(plan.shot_count || shots.length) || 0} 镜／${Number(plan.total_duration_seconds || 0) || 0} 秒`;
  if (plan.creative_conversion) elements.formalPlanSummary.textContent += ` · 保留 ${Number(plan.creative_conversion.mapped_count)} 段初稿 · 新增 ${Number(plan.creative_conversion.added_shot_count)} 镜`;
  elements.formalPlanEvents.innerHTML = events.length ? events.map((event, index) => `<li><span>${escapeHtml(String(event.sequence || index + 1).padStart(2, "0"))}</span><strong>${escapeHtml(event.event || "未提供事件")}</strong></li>`).join("") : '<li class="workbench-empty">暂无可显示的事件。</li>';
  elements.formalPlanCapacity.innerHTML = `<div><span>容量状态</span><strong>${capacity.status === "needs_clarification" ? "需要确认取舍" : "容量可执行"}</strong></div><dl><div><dt>目标时长</dt><dd>${escapeHtml(String(capacity.target_seconds ?? plan.total_duration_seconds ?? "—"))} 秒</dd></div><div><dt>独立内容单元</dt><dd>${escapeHtml(String(capacity.required_shot_units ?? "—"))}</dd></div><div><dt>规划镜头</dt><dd>${escapeHtml(String(capacity.planned_shot_count ?? plan.shot_count ?? "—"))}</dd></div></dl>${(capacity.choices || []).length ? `<p>可选处理：${escapeHtml(capacity.choices.join("／"))}</p>` : ""}`;
  const templateCount = (plan.template_summary?.original_segments || []).reduce((sum, item) => sum + Number(item.shot_count || 0), 0);
  const plannedCount = Number(plan.shot_count || shots.length);
  const quantityBasis = capacity.requested_shot_count != null ? `用户指定 ${capacity.requested_shot_count} 镜`
    : templateCount && templateCount !== plannedCount ? `容量调整：模板 ${templateCount} 镜 → 本次 ${plannedCount} 镜`
    : capacity.hard_slot_quantity_mode === "project_image_capacity" ? "按当前可用图片容量规划"
    : `模板默认${templateCount ? ` ${templateCount} 镜` : ""}`;
  elements.formalPlanCapacity.insertAdjacentHTML("beforeend", `<p>镜数依据：${escapeHtml(quantityBasis)}</p>${capacity.mismatch_reason ? `<p>${escapeHtml(capacity.mismatch_reason)}</p>` : ""}`);
  elements.formalPlanDecisionCounts.innerHTML = planDecisionCountsMarkup(decisionCounts);
  renderFormalPlanConflicts(conflicts);
  elements.formalPlanCoverage.innerHTML = traceability.length ? `<h3>需求与镜头对应</h3>${planTraceTable(traceability)}` : coverage.length ? `<h3>需求覆盖</h3><div class="ui00-plan-coverage-scroll"><table><thead><tr><th>资料要求</th><th>来源</th><th>镜头</th><th>状态</th></tr></thead><tbody>${coverage.map((item) => {
    const requirement = requirementsById[item.requirement_id] || events.find((event) => event.event_id === item.requirement_id) || {};
    const source = (item.evidence_refs || []).map((ref) => planSourceLabel(ref.source || ref.source_ref || ref)).filter(Boolean).join("；") || "当前 Brief";
    const status = ({ covered: "已覆盖", pending: "待处理", blocked: "需修改" })[item.status] || "待核对";
    return `<tr><td>${escapeHtml(requirement.text || requirement.event || "未提供")}</td><td>${escapeHtml(source)}</td><td>${escapeHtml((item.shot_nos || []).join("、") || "—")}</td><td><span data-coverage-status="${escapeHtml(item.status || "unknown")}">${escapeHtml(status)}</span></td></tr>`;
  }).join("")}</tbody></table></div>` : '<div class="workbench-empty">暂无需求覆盖记录。</div>';
  elements.formalPlanShots.innerHTML = shots.length ? shots.map((shot, index) => {
    const source = planSourceLabel(shot.source);
    const creativeNote = shot.creative_block ? `<p class="creative-input-notice">保留初稿：${escapeHtml(shot.creative_block.section)} · 原时长：${escapeHtml(shot.creative_block.original_duration)} · 规划时长：${escapeHtml(String(shot.duration_seconds))} 秒${shot.creative_order_note ? `<br>${escapeHtml(shot.creative_order_note)}` : ""}</p>` : plan.creative_conversion ? '<p class="creative-input-notice">根据确认资料新增的镜头，请核对。</p>' : "";
    return `<article class="ui00-plan-shot"><header><span>镜头 ${escapeHtml(shot.shot_no || String(index + 1))}</span><b>${escapeHtml(String(shot.duration_seconds || "—"))} 秒</b></header>${creativeNote}<dl><div><dt>内容</dt><dd>${escapeHtml(shot.primary_event || "未提供")}</dd></div><div><dt>结构</dt><dd>${escapeHtml(shot.structure || "流程推进")}</dd></div><div><dt>位置</dt><dd>${escapeHtml(planPositionLabel(shot.position_rule || {}))}</dd></div><div><dt>画面</dt><dd>${escapeHtml(shot.visual || (shot.visual_status === "pending_draft" ? "生成脚本时展开本镜画面" : "未提供"))}</dd></div><div><dt>${shot.sound_status === "template_reference" ? "声音参考" : "声音"}</dt><dd>${escapeHtml(shot.sound || "未提供")}${shot.sound_status === "template_reference" ? "（模板建议，生成时按已确认需求改写）" : ""}</dd></div></dl><details><summary>查看采用依据</summary><p>${escapeHtml(source)}</p>${shot.structural_adaptation ? `<p>${escapeHtml(shot.structural_adaptation)}</p>` : ""}</details><details class="ui00-plan-field-panel"><summary>查看字段取舍</summary>${planFieldDecisions(shot.field_decisions || [])}</details></article>`;
  }).join("") : '<div class="workbench-empty">暂无可显示的镜头。</div>';
  const audit = plan.audit || {};
  const auditPassed = ["pass", "passed"].includes(audit.status);
  const auditStatus = auditPassed ? "机器校验已完成，非阻断提示留待人工核对。" : audit.status ? "机器校验提示仍需处理，请先核对规划内容。" : "规划已准备好，仍需人工核对规划内容。";
  const findings = audit.findings || [];
  const planBriefLink = formalPath("brief", formalRoute().projectId, {return_to: "plan", creative: new URLSearchParams(window.location.search).get("creative") || ""});
  const findingMarkup = finding => {
    const location = [finding.fields?.join("、"), finding.shot_no ? `第 ${finding.shot_no} 镜` : "", ...(finding.event_ids || []).map(id => events.find(event => event.event_id === id)?.event || id)].filter(Boolean).join(" · ");
    return `<li><strong>${finding.severity === "warning" ? "提醒" : "待处理"}：${escapeHtml(finding.message || finding.summary || finding.code)}</strong>${location ? `<p>位置：${escapeHtml(location)}</p>` : ""}${finding.risk ? `<p>影响：${escapeHtml(finding.risk)}</p>` : ""}${["fact_missing", "event_missing"].includes(finding.code) ? `<a class="text-button" href="${planBriefLink}">补充资料后返回此规划</a>` : ""}</li>`;
  };
  const blockers = findings.filter(item => item.severity !== "warning"), warnings = findings.filter(item => item.severity === "warning");
  elements.formalPlanAudit.innerHTML = `<p>${escapeHtml(auditStatus)}</p>${blockers.length ? `<ul>${blockers.map(findingMarkup).join("")}</ul>` : ""}${warnings.length ? `<details><summary>查看 ${warnings.length} 项人工核对提示</summary><ul>${warnings.map(findingMarkup).join("")}</ul></details>` : ""}${planHasDecisionGate(plan) ? `<p>处理上述缺项或冲突后重新生成规划；原规划保留为历史。</p><a href="${planBriefLink}" class="text-button">核对需求并返回规划</a>` : ""}`;
  const clarification = planHasDecisionGate(plan);
  elements.formalPlanConfirmCheck.disabled = clarification;
  elements.formalPlanConfirm.disabled = true;
  elements.formalPlanPreview.hidden = false;
  elements.formalPlanResult.hidden = true;
  elements.formalPlanPreview.querySelectorAll("details").forEach(item => { if (expanded.has(item.id || item.querySelector("summary")?.textContent)) item.open = true; });
  elements.formalPlanConflictList.querySelectorAll("[data-conflict-id]").forEach(card => { const value = choices.get(card.dataset.conflictId); if (value) { const input = [...card.querySelectorAll("input")].find(item => item.value === value); if (input) input.checked = true; } });
}

function selectedPlanConflictResolutions() {
  const cards = [...elements.formalPlanConflictList.querySelectorAll("[data-conflict-id]")];
  const resolutions = cards.map((card) => ({
    conflict_id: card.dataset.conflictId,
    choice: card.querySelector('input[type="radio"]:checked')?.value || "",
  }));
  return resolutions.every((item) => item.conflict_id && item.choice) ? resolutions : [];
}

async function prepareFormalPlanConflicts() {
  const projectId = formalRoute().projectId;
  const plan = state.formalPlan;
  const resolutions = selectedPlanConflictResolutions();
  const unresolvedCount = Number(plan?.decision_counts?.unresolved_conflicts || 0);
  if (!projectId || !plan?.plan_id || state.formalPlanBusy) return;
  if (!resolutions.length || resolutions.length !== unresolvedCount) {
    showToast("请先为每一项未解决冲突选择处理方式。");
    return;
  }
  state.formalPlanBusy = true;
  const viewRequest = state.formalPlanRequest;
  elements.formalPlanConflictPrepare.disabled = true;
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    if (!isCurrentFormalPlan(projectId, viewRequest)) return;
    const request = {
      plan_sha256: plan.plan_sha256,
      expected_project_version: project.project_version,
      idempotency_key: formalRequestKey("plan-conflict"),
      resolutions,
    };
    const prepared = await api(formalPlanConflictPath(projectId, plan.plan_id, "prepare"), {
      method: "POST", body: JSON.stringify(request),
    });
    if (!isCurrentFormalPlan(projectId, viewRequest)) return;
    state.pendingPlanConflictAction = { prepared, request, projectId, planId: plan.plan_id };
    elements.formalPlanConflictWillChange.textContent = prepared.will_change || "保存冲突决定并重新规划。";
    elements.formalPlanConflictWillNotChange.textContent = prepared.will_not_change || "不覆盖原 Brief、原 Plan 或历史 run。";
    elements.formalPlanConflictConfirmCopy.textContent = `${prepared.confirmation_label || "请核对全部选择"}。模型调用 ${Number(prepared.model_calls || 0)} 次。`;
    elements.formalPlanConflictConfirmation.hidden = false;
    elements.formalPlanConflictConfirmation.focus();
  } catch (error) {
    if (!isCurrentFormalPlan(projectId, viewRequest)) return;
    const code = error?.payload?.detail?.code;
    if (["PROJECT_VERSION_CONFLICT", "PLAN_ARTIFACT_DRIFT"].includes(code)) await loadFormalPlan(projectId);
    else showToast(formalError(error));
  } finally {
    if (isCurrentFormalPlan(projectId, viewRequest)) {
      state.formalPlanBusy = false;
      elements.formalPlanConflictPrepare.disabled = false;
    }
  }
}

function cancelFormalPlanConflictConfirmation() {
  state.pendingPlanConflictAction = null;
  elements.formalPlanConflictConfirmation.hidden = true;
  elements.formalPlanConflictCheck.checked = false;
  elements.formalPlanConflictExecute.disabled = true;
}

function locatePlanDecision(event) {
  const button = event.target.closest("[data-plan-decision-target]");
  if (!button || button.disabled) return;
  const target = button.dataset.planDecisionTarget;
  const destination = target === "coverage" ? elements.formalPlanCoverage
    : target === "conflicts" ? elements.formalPlanConflicts
    : target === "review-trace" ? elements.formalReviewTraceability.closest("section")
    : elements.formalPlanShots;
  const details = destination?.closest("details");
  if (details) details.open = true;
  destination?.scrollIntoView({ behavior: "smooth", block: "start" });
  destination?.focus?.({ preventScroll: true });
}

async function executeFormalPlanConflicts() {
  const pending = state.pendingPlanConflictAction;
  if (!pending || !elements.formalPlanConflictCheck.checked || state.formalPlanBusy) return;
  state.formalPlanBusy = true;
  const request = state.formalPlanRequest;
  elements.formalPlanConflictExecute.disabled = true;
  elements.formalPlanConflictExecute.textContent = "正在重新规划…";
  try {
    const result = await api(formalPlanConflictPath(pending.projectId, pending.planId, "execute"), {
      method: "POST",
      body: JSON.stringify({
        ...pending.request,
        command_token: pending.prepared.command_token,
      }),
    });
    if (!isCurrentFormalPlan(pending.projectId, request)) return;
    if (!result.new_plan?.plan_id) throw new Error("重新规划未返回可核对的 Plan。");
    state.pendingPlanConflictAction = null;
    renderFormalPlanPreview(result.new_plan);
    setFormalPlanState("waiting_user", "冲突决定已保存，请核对新规划。");
    elements.formalPlanConfirmation.hidden = false;
    elements.formalPlanPreview.scrollIntoView({ behavior: "smooth", block: "start" });
    showToast("冲突已按确认方案重新规划；未调用模型。");
  } catch (error) {
    if (!isCurrentFormalPlan(pending.projectId, request)) return;
    const code = error?.payload?.detail?.code;
    if (["PROJECT_VERSION_CONFLICT", "PLAN_ARTIFACT_DRIFT", "PERMISSION_DENIED"].includes(code)) await loadFormalPlan(pending.projectId);
    else showToast(error?.payload ? formalError(error) : error.message);
  } finally {
    if (isCurrentFormalPlan(pending.projectId, request)) {
      state.formalPlanBusy = false;
      elements.formalPlanConflictExecute.textContent = "确认并重新规划";
    }
  }
}

function renderFormalPlanResult(result) {
  creativeState().loaded = false;
  creativeState().imageKey = "";
  const rows = Array.isArray(result.script_rows) ? result.script_rows : [];
  elements.formalPlanScriptRows.innerHTML = rows.length ? `<table><thead><tr><th scope="col">镜头</th><th scope="col">画面</th><th scope="col">声音</th><th scope="col">时长</th></tr></thead><tbody>${rows.map((row, index) => `<tr><td>${escapeHtml(row["镜号"] || String(index + 1))}</td><td>${escapeHtml(row["画面描述"] || row.visual || "未提供")}</td><td>${escapeHtml(row["声音"] || row.sound || "未提供")}</td><td>${escapeHtml(String(row["时长(秒)"] || row.duration_seconds || "—"))} 秒</td></tr>`).join("")}</tbody></table>` : '<div class="workbench-empty">脚本预览暂不可用，请进入脚本与版本查看。</div>';
  const pending = Array.isArray(result.pending_confirmations) ? result.pending_confirmations : [];
  if (pending.length) elements.formalPlanScriptRows.insertAdjacentHTML("beforeend", `<div class="ui00-plan-audit">待确认事项：${escapeHtml(pending.map((item) => typeof item === "string" ? item : `${item.shot_no ? `第 ${item.shot_no} 镜：` : ""}${item.question || item.message || item.item || item.fact || "待人工核对"}`).join("；"))}</div>`);
  if (result.creative_images?.message) elements.formalPlanScriptRows.insertAdjacentHTML("beforeend", `<p role="status">${escapeHtml(result.creative_images.message)}</p>`);
  elements.formalPlanResult.hidden = false;
  elements.formalPlanConfirmation.hidden = true;
}

function planReadiness(currentBrief) {
  const confirmed = currentBrief?.status !== "empty" && currentBrief?.confirmation_status === "confirmed";
  const standard = currentBrief?.creative_standard;
  return {
    confirmed,
    standardReady: Boolean(standard?.status === "confirmed"),
    standardUnavailable: standard?.status === "unavailable",
    routingReady: currentBrief?.routing?.status === "ready",
    routing: currentBrief?.routing || {},
  };
}

function enableFormalReplanning(message, usesCreative = false) {
  if (usesCreative) {
    const url = new URL(window.location.href);
    url.searchParams.set("creative", "1");
    window.history.replaceState({}, "", url.pathname + url.search);
  }
  state.formalPlan = null;
  elements.formalPlanConfirmation.hidden = true;
  setFormalPlanState("idle", message);
  elements.formalPlanCreate.textContent = "重新生成规划";
  elements.formalPlanAction.hidden = false;
}

function renderCreativeCurrentStructure(model) {
  const shots = Array.isArray(model?.shots) ? model.shots : [];
  elements.creativeCurrentStructure.hidden = !shots.length;
  if (!shots.length) { elements.creativeCurrentStructure.replaceChildren(); return; }
  const label = model.can_save_version ? `当前草稿 · 基于${model.version_label || "已保存版本"}` : `${model.version_label || "当前版本"} · 已保存`;
  elements.creativeCurrentStructure.innerHTML = `<div class="panel-kicker"><span>当前结构</span><b>${escapeHtml(label)} · ${shots.length} 镜</b></div><p>按当前稿顺序显示。修改镜头内容请切换到脚本。</p><div class="ui00-plan-shots">${shots.map(shot => `<article class="ui00-plan-shot" data-structure-shot-id="${escapeHtml(shot.shot_id)}"><header><span>镜头 ${escapeHtml(shot.shot_no)}</span><b>${escapeHtml(String(shot.duration_seconds ?? "—"))} 秒</b></header><dl><div><dt>结构</dt><dd>${escapeHtml(shot.structure || "未提供")}</dd></div><div><dt>画面</dt><dd>${escapeHtml(shot.visual || "未提供")}</dd></div></dl><details><summary>查看来源</summary><p>${escapeHtml(planSourceLabel(shot.source))}</p></details></article>`).join("")}</div>`;
}

function isCurrentFormalPlan(projectId, request) {
  return request === state.formalPlanRequest && formalRoute().projectId === projectId && formalSurface() === "plan";
}

async function loadFormalPlan(projectId) {
  if (currentInitialDraft(projectId)) {
    const url = new URL(window.location.href);
    url.searchParams.set('creative', '1');
    window.history.replaceState({}, '', url.pathname + url.search);
  }
  const available = Boolean(projectId && state.formalBriefAvailable && state.agentAuth && state.agentWorkspaceId);
  elements.formalPlanProjectRequired.hidden = available;
  elements.formalPlanWorkbench.hidden = !available;
  resetFormalPlan();
  state.formalPlanProjectId = projectId;
  const request = state.formalPlanRequest;
  renderCreativeCurrentStructure(available && creativeState().projectId === projectId ? state.formalScript : null);
  if (available) await loadFormalTemplates(projectId, "plan");
  if (!isCurrentFormalPlan(projectId, request)) return;
  state.formalPlanTextOnly = new URLSearchParams(window.location.search).get("mode") === "text-only";
  if (!available) { setFormalPlanState("blocked", "请先从项目列表进入项目。"); return; }
  const returnQuery = {return_to: "plan", creative: new URLSearchParams(window.location.search).get("creative") || ""};
  for (const block of [elements.formalPlanBriefBlock, elements.formalPlanStandardBlock])
    block.querySelectorAll("a").forEach(link => { link.href = formalPath("brief", projectId, returnQuery); });
  setFormalPlanState("loading", "正在核对 Brief、创作标准卡与已有规划…");
  try {
    const [project, currentBrief] = await Promise.all([
      api(`/api/formal/projects/${encodeURIComponent(projectId)}`),
      api(`/api/formal/projects/${encodeURIComponent(projectId)}/brief/current?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`),
    ]);
    if (!isCurrentFormalPlan(projectId, request)) return;
    const readiness = planReadiness(currentBrief);
    if (!readiness.confirmed) {
      setFormalPlanState("blocked", "请先完成并确认需求，再继续项目规划。");
      elements.formalPlanBriefBlock.hidden = false;
      return;
    }
    if (!readiness.standardReady) {
      const copy = readiness.standardUnavailable ? "需重新导入资料生成标准卡。" : "请先完成并确认项目创作标准卡。";
      setFormalPlanState("blocked", copy);
      elements.formalPlanStandardBlockTitle.textContent = readiness.standardUnavailable ? "需重新导入资料生成标准卡" : "请先完成并确认项目创作标准卡";
      elements.formalPlanStandardBlockCopy.textContent = readiness.standardUnavailable ? "当前资料来自旧记录。请重新导入资料并确认需求。" : "创作标准卡需要与当前已确认的 Brief 保持同一版本。完成确认后，再生成规划。";
      elements.formalPlanStandardBlock.hidden = false;
      return;
    }
    if (!readiness.routingReady) {
      setFormalPlanState("blocked", readiness.routing.message || "当前 Brief 尚未匹配可用的生成模板。");
      elements.formalPlanBriefBlock.querySelector("strong").textContent = "请先确认生成模板";
      elements.formalPlanBriefBlock.querySelector("p").textContent = readiness.routing.message || "返回需求页补充可识别的活动类型。";
      elements.formalPlanBriefBlock.hidden = false;
      return;
    }
    const planningReadiness = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/planning-readiness?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(project.project_version)}`);
    if (!isCurrentFormalPlan(projectId, request)) return;
    if (planningReadiness.ready !== true) {
      const message = planningReadiness.message || "当前规划资料尚未核对完成，请返回需求查看。";
      setFormalPlanState("blocked", message);
      elements.formalPlanBriefBlock.querySelector("strong").textContent = "请补充规划资料";
      elements.formalPlanBriefBlock.querySelector("p").textContent = message;
      elements.formalPlanBriefBlock.hidden = false;
      return;
    }
    const imageReadiness = await api(formalImageCollectionPath(projectId, project.project_version));
    if (!isCurrentFormalPlan(projectId, request)) return;
    const creativeReferenceCount = new URLSearchParams(window.location.search).get("creative") === "1"
      ? Number(imageReadiness.creative_reference_count || 0) : 0;
    const creativeReferenceCopy = creativeReferenceCount > 0
      ? `初稿已保留 ${creativeReferenceCount} 张参考图（${Number(imageReadiness.creative_reference_block_count || 0)} 段），生成正式稿后仍需逐镜核对。` : "";
    elements.formalPlanImageBlock.dataset.creativeReferenceCopy = creativeReferenceCopy;
    const imageLinks = elements.formalPlanImageBlock.querySelectorAll("a");
    imageLinks[0].href = formalPath("materials", projectId, {
      return_to: "plan", ...(state.formalPlanTextOnly ? { mode: "text-only" } : {}),
      ...(new URLSearchParams(window.location.search).get("creative") === "1" ? { creative: "1" } : {}),
    });
    imageLinks[0].dataset.formalTarget = "1";
    imageLinks[1].href = state.formalPlanTextOnly
      ? formalPath("brief", projectId)
      : formalPath("plan", projectId, { mode: "text-only", ...(new URLSearchParams(window.location.search).get("creative") === "1" ? { creative: "1" } : {}) });
    imageLinks[1].textContent = state.formalPlanTextOnly ? "查看 Brief" : "仅生成文字草稿";
    imageLinks[1].dataset.formalTarget = "1";
    if (!state.formalPlanTextOnly && !formalImagesReady(imageReadiness)) {
      const attention = Number(imageReadiness.attention_count ?? 0) || (
        Number(imageReadiness.pending || 0) + Number(imageReadiness.low_confidence || 0)
        + Number(imageReadiness.blocked || 0) + Number(imageReadiness.failed || 0)
      );
      setFormalPlanState("blocked", "当前没有可用于规划的项目图片。");
      elements.formalPlanImageBlockCopy.textContent = `${creativeReferenceCopy}还有 ${Number(imageReadiness.pending || 0)} 张待理解，${attention} 张需要关注。请先获得至少一张可用图片，或明确选择文字草稿。`;
      elements.formalPlanImageBlock.hidden = false;
      return;
    }
    state.formalPlanZeroImage = Number(imageReadiness.total || 0) === 0;
    if (state.formalPlanZeroImage || state.formalPlanTextOnly) {
      elements.formalPlanImageBlockTitle.textContent = state.formalPlanTextOnly ? "文字草稿" : creativeReferenceCopy ? "初稿参考图已保留" : "当前没有图片";
      elements.formalPlanImageBlockCopy.textContent = creativeReferenceCopy
        ? `${creativeReferenceCopy}${state.formalPlanZeroImage ? "当前没有已确认项目图片，可先生成文字草稿并保留这些参考。" : "可先生成文字草稿并保留这些参考；补齐正式图片并审核后交付。"}`
        : state.formalPlanTextOnly ? "可先生成和编辑；补齐图片并审核后交付。" : "请前往项目素材选图，或选择文字草稿。";
      elements.formalPlanImageBlock.hidden = false;
      elements.formalPlanCreate.textContent = state.formalPlanTextOnly ? "生成文字规划" : "选择文字草稿";
    }
    const listed = await api(formalPlanCollectionPath(projectId));
    if (!isCurrentFormalPlan(projectId, request)) return;
    const waiting = (listed.items || []).find((item) => item.status === "waiting_user");
    if (waiting?.plan_id) {
      try {
        const preview = await api(formalPlanPreviewPath(projectId, waiting.plan_id, project.project_version));
        if (!isCurrentFormalPlan(projectId, request)) return;
        if (useCurrentCreativePlan(projectId) && !preview.creative_conversion) {
          enableFormalReplanning('当前已有初稿，请按当前初稿重新生成规划；已有文字、顺序和参考图会保留。', true);
          return;
        }
        renderFormalPlanPreview(preview);
        setFormalPlanState("waiting_user", "已恢复待确认的规划。核对后再创建脚本版本。");
        elements.formalPlanConfirmation.hidden = false;
        return;
      } catch (error) {
        if (!isCurrentFormalPlan(projectId, request)) return;
        const code = error?.payload?.detail?.code;
        if (!["CREATIVE_PLAN_STALE", "PLAN_INPUTS_STALE", "KNOWLEDGE_RULE_DRIFT", "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED"].includes(code)) throw error;
        enableFormalReplanning(code === "CREATIVE_PLAN_STALE" ? "初稿已更新，请按最新初稿重新生成规划。" : formalError(error), code === "CREATIVE_PLAN_STALE");
        return;
      }
    }
    const unavailable = listed.unavailable_items || [];
    if (unavailable.length) {
      enableFormalReplanning(unavailable.map(item => item.message).filter((value, index, all) => value && all.indexOf(value) === index).join(" "),
        unavailable[0].uses_creative_draft && !state.formalScript?.base_version_id);
      return;
    }
    setFormalPlanState("idle", `需求与创作标准已确认，可以生成规划；将采用${readiness.routing.script_type}模板。`);
    if (!elements.creativeCurrentStructure.hidden) {
      elements.formalPlanState.textContent = "当前结构已读取";
      elements.formalPlanStatusCopy.textContent = "当前稿的镜头安排如下。需要调整整体编排时，可以生成新的规划。";
    }
    elements.formalPlanAction.hidden = false;
  } catch (error) {
    if (!isCurrentFormalPlan(projectId, request)) return;
    setFormalPlanState("blocked", formalError(error));
  }
}

async function createFormalPlan() {
  const projectId = formalRoute().projectId;
  if (!projectId || state.formalPlanBusy || state.formalPlanState !== "idle") return;
  const request = state.formalPlanRequest;
  const initial = currentInitialDraft(projectId);
  if (initial) {
    if (firstCreativePending(initial)) { showToast('当前初稿仍在处理，请完成后再生成规划。'); return; }
    if (!await saveFirstCreative(initial)) { setFormalPlanState('idle', '初稿修改尚未保存。请返回脚本处理保存提示后继续。'); return; }
    if (!isCurrentFormalPlan(projectId, request) || state.formalPlanBusy || state.formalPlanState !== 'idle') return;
  }
  if (state.formalPlanZeroImage && !state.formalPlanTextOnly) {
    state.formalPlanTextOnly = true;
    window.history.replaceState({}, "", formalPath("plan", { mode: "text-only", ...(new URLSearchParams(window.location.search).get("creative") === "1" ? { creative: "1" } : {}) }));
    elements.formalPlanImageBlockTitle.textContent = "文字草稿";
    const creativeReferenceCopy = elements.formalPlanImageBlock.dataset.creativeReferenceCopy || "";
    elements.formalPlanImageBlockCopy.textContent = creativeReferenceCopy
      ? `${creativeReferenceCopy}可先生成文字草稿并保留这些参考；补齐正式图片并审核后交付。`
      : "可先生成和编辑；补齐图片并审核后交付。";
    elements.formalPlanCreate.textContent = "生成文字规划";
    return;
  }
  state.formalPlanBusy = true;
  const params = new URLSearchParams(window.location.search);
  const revisionTaskId = params.get("revision_task_id") || null;
  const useCreative = useCurrentCreativePlan(projectId);
  const generationMode = state.formalPlanTextOnly ? "text_only_draft" : "image_backed";
  elements.formalPlanCreate.disabled = true;
  elements.formalPlanCreate.textContent = "正在生成规划…";
  setFormalPlanState("loading", "正在生成可核对的事件与镜头规划…");
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    if (!isCurrentFormalPlan(projectId, request)) return;
    const creativeInput = useCreative ? await api(firstCreativePath(firstCreativeView(projectId))) : null;
    if (!isCurrentFormalPlan(projectId, request)) return;
    const preview = await api(formalPlanCollectionPath(projectId), { method: "POST", body: JSON.stringify({
      expected_project_version: project.project_version,
      generation_mode: generationMode,
      revision_task_id: revisionTaskId,
      ...(creativeInput ? { expected_creative_revision: creativeInput.revision } : {}),
    }) });
    if (!isCurrentFormalPlan(projectId, request)) return;
    renderFormalPlanPreview(preview);
    setFormalPlanState("waiting_user", "规划已生成，请先核对内容再确认。");
    elements.formalPlanAction.hidden = true;
    elements.formalPlanConfirmation.hidden = false;
    elements.formalPlanConfirmation.scrollIntoView({ behavior: "smooth", block: "center" });
    elements.formalPlanConfirmation.focus({ preventScroll: true });
  } catch (error) {
    if (!isCurrentFormalPlan(projectId, request)) return;
    if (error?.payload?.detail?.code === "PROJECT_VERSION_CONFLICT") await loadFormalPlan(projectId);
    else setFormalPlanState("blocked", formalError(error));
  } finally {
    if (isCurrentFormalPlan(projectId, request)) {
      state.formalPlanBusy = false;
      elements.formalPlanCreate.disabled = state.formalPlanState !== "idle";
      elements.formalPlanCreate.textContent = "生成规划";
    }
  }
}

async function confirmFormalPlan() {
  const projectId = formalRoute().projectId;
  if (!projectId || !state.formalPlan?.plan_id || planHasDecisionGate(state.formalPlan) || state.formalPlanBusy || !elements.formalPlanConfirmCheck.checked) return;
  state.formalPlanBusy = true;
  let request = state.formalPlanRequest;
  const planId = state.formalPlan.plan_id;
  elements.formalPlanConfirm.disabled = true;
  elements.formalPlanConfirm.textContent = "正在创建脚本版本…";
  setFormalPlanState("generating", "正在创建脚本版本，请勿重复操作。");
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    if (!isCurrentFormalPlan(projectId, request)) return;
    const result = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/plans/${encodeURIComponent(planId)}/confirm?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, { method: "POST", body: JSON.stringify({ expected_project_version: project.project_version }) });
    if (!isCurrentFormalPlan(projectId, request)) return;
    if (!Array.isArray(result.script_rows) || !result.script_rows.length) throw new Error("脚本版本未返回可查看的内容，请刷新后重试。");
    renderFormalPlanResult(result);
    state.uiInputBaselines.delete(elements.formalPlanConfirmCheck);
    setFormalPlanState("completed", "脚本版本已创建，可进入脚本与版本继续编辑。");
    await loadFormalScript(projectId);
    if (document.querySelector("#workbench-recommend-after-formal-generation").checked) { rememberWorkbenchRecommendation("formal", state.formalScript.base_version_id); void maybeRecommendWorkbenchGeneration(); }
  } catch (error) {
    if (!isCurrentFormalPlan(projectId, request)) return;
    const code = error?.payload?.detail?.code;
    if (["PROJECT_VERSION_CONFLICT", "PLAN_INPUTS_STALE", "CREATIVE_PLAN_STALE", "KNOWLEDGE_RULE_DRIFT", "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED"].includes(code)) {
      await loadFormalPlan(projectId);
      if (formalRoute().projectId !== projectId) return;
      request = state.formalPlanRequest;
      setFormalPlanState("blocked", formalError(error));
    }
    else if (["FORMAL_GENERATION_BLOCKED", "FORMAL_GENERATION_AUDIT_FAILED"].includes(code)) {
      await loadFormalPlan(projectId);
      if (formalRoute().projectId !== projectId) return;
      request = state.formalPlanRequest;
      const message = `${formalError(error)} 本次未创建版本，失败记录已保留。请重新生成规划后继续。`;
      if (state.formalPlanState === "idle") enableFormalReplanning(message);
      else setFormalPlanState("blocked", message);
      elements.formalPlanConfirmation.hidden = true;
    }
    else {
      setFormalPlanState("waiting_user", error?.payload ? formalError(error) : error.message);
      elements.formalPlanConfirmation.hidden = false;
    }
  } finally {
    if (isCurrentFormalPlan(projectId, request)) {
      state.formalPlanBusy = false;
      elements.formalPlanConfirm.textContent = "确认并创建脚本版本";
      elements.formalPlanConfirm.disabled = planHasDecisionGate(state.formalPlan) || !elements.formalPlanConfirmCheck.checked || state.formalPlanState !== "waiting_user";
    }
  }
}

function renderFormalConflicts(conflicts, facts) {
  elements.formalConflictCount.textContent = `${conflicts.filter((item) => item.resolution_status !== "resolved").length} 项`;
  if (!conflicts.length) { elements.formalConflictList.innerHTML = '<div class="workbench-empty">没有待确认冲突。</div>'; return; }
  elements.formalConflictList.replaceChildren();
  conflicts.forEach((conflict) => {
    const item = document.createElement("article");
    item.className = "ui00-conflict-item";
    item.dataset.briefConflict = conflict.conflict_key;
    const resolved = conflict.resolution_status === "resolved";
    item.innerHTML = `<strong>${escapeHtml(conflict.label || "资料内容")} · ${resolved ? "已确认" : "待核对"}</strong><small>${resolved ? "当前采用值已进入摘要，其余内容保留在来源历史。" : "以下来源给出了不同内容，请选择本项目采用的值。"}</small><ul class="brief-conflict-candidates">${(conflict.choices || []).map(candidate => `<li><span>${escapeHtml(formalValue(candidate.value))}</span>${resolved && candidate.fact_key === conflict.selected_fact_key ? " · 已采用" : ""}<small data-conflict-source>${escapeHtml((candidate.sources || []).join("；") || "来源位置暂未提供")}</small></li>`).join("")}</ul>`;
    if (!resolved) {
      const choice = document.createElement("div"); choice.className = "ui00-conflict-choice";
      const label = document.createElement("label"); label.textContent = "项目实际采用的内容";
      const select = document.createElement("select");
      (conflict.choices || []).forEach((choiceValue) => {
        const option = document.createElement("option"); option.value = choiceValue.fact_key; option.textContent = formalValue(choiceValue.value); select.append(option);
      });
      label.append(select);
      const button = document.createElement("button"); button.type = "button"; button.className = "secondary-button compact"; button.textContent = "确认选择"; button.disabled = !select.value;
      button.addEventListener("click", () => resolveFormalConflict(conflict.conflict_key, select.value, button));
      choice.append(label, button); item.append(choice);
    }
    elements.formalConflictList.append(item);
  });
}

async function retryFormalBriefFile(button) {
  const projectId = formalRoute().projectId;
  const documentKey = button.dataset.briefFileRetry;
  const briefKey = state.formalBrief?.brief_key;
  if (!projectId || !documentKey || !briefKey) return;
  button.disabled = true;
  const notice = button.closest("details").querySelector("[data-brief-retry-state]");
  notice.textContent = "正在重新读取原文件，其他已读取资料保留…";
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    await api(`/api/formal/projects/${encodeURIComponent(projectId)}/brief/files/retry?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`, {method:"POST", body:JSON.stringify({brief_version_id:briefKey, document_keys:[documentKey], expected_project_version:project.project_version})});
    if (!isFormalBriefProject(projectId)) return;
    await loadFormalBrief(projectId);
    showToast(state.formalBrief?.has_failed_extractions ? "重试已记录，仍未读取的文件可以替换后继续。" : "原文件已重新读取，成功资料与来源已保留，请核对后确认需求。");
  } catch (error) {
    if (notice.isConnected) notice.textContent = `${formalError(error)} 原文件和已有资料已保留。`;
  } finally { if (button.isConnected) button.disabled = false; }
}

function replaceFormalBriefFile(button) {
  const projectId = formalRoute().projectId, briefKey = state.formalBrief?.brief_key;
  const documentKey = button.dataset.briefFileReplace;
  if (!projectId || !briefKey || !documentKey) return;
  let input = document.querySelector("#brief-replace-file");
  if (!input) { input = document.createElement("input"); input.type="file"; input.id="brief-replace-file"; input.hidden=true; document.body.append(input); }
  input.accept = elements.formalBriefFiles.accept;
  input.value = "";
  input.onchange = async () => {
    const file = input.files[0];
    if (!file || !isFormalBriefProject(projectId)) return;
    const notice = button.closest("details").querySelector("[data-brief-retry-state]");
    button.disabled = true; notice.textContent = "正在读取替换文件，原失败记录与其他资料保留…";
    try {
      const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
      const data = new FormData(); data.append("file", file);
      await api(`/api/formal/projects/${encodeURIComponent(projectId)}/brief/files/${encodeURIComponent(documentKey)}/replace?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&brief_version_id=${encodeURIComponent(briefKey)}&expected_project_version=${encodeURIComponent(project.project_version)}`, {method:"POST", body:data});
      if (isFormalBriefProject(projectId)) { await loadFormalBrief(projectId); showToast("替换文件已保存为新的资料修订，请核对后确认需求。"); }
    } catch (error) { if (notice.isConnected) notice.textContent = `${formalError(error)} 原文件和其他资料已保留。`; }
    finally { if (button.isConnected) button.disabled=false; input.value=""; }
  };
  input.click();
}

async function createFormalProject(event) {
  event.preventDefault();
  if (!elements.formalProjectName.value.trim()) {
    showToast("请输入项目名称，不能只填空格。");
    elements.formalProjectName.focus();
    return;
  }
  if (!elements.formalProjectCreate.reportValidity()) return;
  if (elements.formalProjectCreateSubmit.disabled) return;
  elements.formalProjectCreateSubmit.disabled = true;
  elements.formalProjectCreateSubmit.textContent = "创建中…";
  try {
    const project = await api(`/api/formal/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/projects`, { method: "POST", body: JSON.stringify({ name: elements.formalProjectName.value.trim(), expected_workspace_version: state.formalWorkspaceVersion }) });
    elements.formalProjectCreate.hidden = true;
    elements.formalProjectName.value = "";
    await loadFormalProjects();
    navigateFormal(formalPath("brief", project.project_id));
  } catch (error) { showToast(formalError(error)); }
  finally { elements.formalProjectCreateSubmit.disabled = false; elements.formalProjectCreateSubmit.textContent = "创建项目"; }
}

async function uploadFormalBrief(event) {
  event.preventDefault();
  const projectId = formalRoute().projectId;
  if (!isFormalBriefProject(projectId) || !elements.formalBriefUpload.reportValidity()) return;
  const input = formalBriefInput(projectId);
  if (input.busy) return;
  rememberFormalBriefInput();
  if (input.mode !== 'details' && (input.answerRequest || Object.values(input.questionAnswers || {}).some(value => value?.trim()))) {
    const attached = input.files.length > 0;
    if (await submitBriefAnswers(projectId, input, {analyze: !attached}) && (!attached || !input.lastAnswersSaved)) return;
    if (!isFormalBriefProject(projectId)) return;
  }
  const revision = input.revision;
  const files = [...elements.formalBriefFiles.files];
  const directText = briefIntakeText();
  if (!files.length && !directText) {
    showToast("请拖入资料、选择文件，或直接粘贴 Brief 文字。");
    elements.formalBriefText.focus();
    return;
  }
  const uploadProblem = formalBriefUploadProblem(files, directText, state.formalBriefUploadLimits);
  if (uploadProblem) {
    showToast(uploadProblem);
    return;
  }
  const editingField = elements.formalBriefUpload.dataset.editFactField || "";
  const categoryChoice = elements.formalProjectCategory.value;
  input.busy = true;
  const button = elements.formalBriefUploadButton; button.disabled = true; button.textContent = "正在保存…";
  document.querySelector("#brief-intake-status").textContent = "正在读取并保存本次资料…";
  let imported = false;
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    const data = new FormData();
    files.forEach((file) => data.append("files", file));
    if (directText) data.append("text", directText);
    data.append("critical", "true");
    const path = `/api/formal/projects/${encodeURIComponent(projectId)}/brief/files?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}&expected_project_version=${encodeURIComponent(project.project_version)}`;
    const result = await api(path, { method: "POST", body: data });
    imported = true;
    if (input.drafts[editingField]?.text === directText || input.drafts[editingField]?.text?.trim() === directText) delete input.drafts[editingField];
    if (input.revision === revision) {
      input.text = "";
      input.files = [];
      input.editingField = "";
      input.details = {};
      if (editingField === "脚本类型" && elements.formalProjectCategory.value === categoryChoice) state.uiInputBaselines.delete(elements.formalProjectCategory);
    }
    persistFormalBriefInput(projectId, input);
    if (!isFormalBriefProject(projectId)) return;
    restoreFormalBriefInput(input);
    await loadFormalBrief(projectId);
    if (!isFormalBriefProject(projectId)) return;
    await analyzeStoredBrief(projectId, [files.length ? `已上传：${files.map(file => file.name).join("、")}` : "", directText].filter(Boolean).join("\n") || "请分析当前需求");
  } catch (error) {
    if (isFormalBriefProject(projectId)) {
      document.querySelector("#brief-intake-status").textContent = `${imported ? "资料已保存，分析尚未完成。" : "本次录入未完成，输入和文件已保留。"}${formalError(error)}`;
      document.querySelector("#brief-analysis-retry").hidden = !imported;
      if (imported) await loadBriefConversation(projectId);
    }
  }
  finally {
    input.busy = false;
    if (isFormalBriefProject(projectId)) {
      button.disabled = false;
      button.textContent = input.editingField ? "保存并重新分析" : "发送";
    }
  }
}

function beginFormalFactEdit(button) {
  if (!isFormalBriefProject(formalRoute().projectId)) return;
  const field = String(button.dataset.editFormalFact || "").trim();
  const value = String(button.dataset.editFormalValue || "").trim();
  if (!field) return;
  rememberFormalBriefInput();
  const input = formalBriefInput(state.formalBriefProjectId);
  input.mode = "conversation"; renderBriefMode(input);
  const retained = input.drafts[field];
  if (!retained) input.drafts[field] = {text: `${field}: ${value}`, baseline: value};
  elements.formalBriefUpload.dataset.editFactField = field;
  elements.formalBriefText.value = input.drafts[field].text;
  if (retained?.baseline !== undefined && retained.baseline !== value)
    showToast("此字段的服务端内容已变化，已恢复你的未提交草稿，请核对后保存。最新内容：" + value);
  renderFormalBriefSelection();
  rememberFormalBriefInput();
  formalBriefInput(state.formalBriefProjectId).revision += 1;
  elements.formalBriefUploadButton.textContent = "保存并重新分析";
  renderInlineFormalFact(button, input);
  button.closest("article").querySelector("[data-inline-fact-value]").focus({preventScroll:true});
}

function renderInlineFormalFact(button, input) {
  const card = button.closest("article"), field = button.dataset.editFormalFact;
  if (card.querySelector("[data-inline-fact]")) return;
  const raw = input.drafts[field]?.text || `${field}: ${button.dataset.editFormalValue}`;
  const value = raw.startsWith(field) ? raw.slice(field.length).replace(/^[：:]\s*/, "") : raw;
  button.hidden = true;
  card.insertAdjacentHTML("beforeend", `<form class="brief-inline-edit" data-inline-fact="${escapeHtml(field)}"><label>修改${escapeHtml(field)}<textarea data-inline-fact-value rows="3" required>${escapeHtml(value)}</textarea></label><p role="status">${input.drafts[field]?.baseline !== button.dataset.editFormalValue ? "服务端内容有变化，你的未提交输入已保留，请对照摘要核对。" : "保存后保留原来源与历史内容，请重新确认需求。"}</p><div class="brief-inline-edit-actions"><button class="secondary-button compact" type="submit">保存此字段</button><button class="text-button" type="button" data-inline-fact-close>收起编辑</button><button class="text-button" type="button" data-inline-fact-details>修改多个字段</button></div></form>`);
}

async function saveInlineFormalFact(form) {
  const projectId = formalRoute().projectId, field = form.dataset.inlineFact;
  if (!isFormalBriefProject(projectId) || !form.reportValidity()) return;
  const workspaceId = state.agentWorkspaceId, briefKey = state.formalBrief?.brief_key;
  const value = form.querySelector("textarea").value.trim(), submitted = `${field}: ${value}`;
  const button = form.querySelector('[type="submit"]'), notice = form.querySelector('[role="status"]');
  if (button.disabled) return;
  if (!value) {
    notice.textContent = `${field}不能为空或只填空格，请补充实际内容后保存。`;
    form.querySelector("textarea").focus();
    return;
  }
  button.disabled=true; notice.textContent="正在保存此字段…";
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    await api(`/api/formal/projects/${encodeURIComponent(projectId)}/brief/field?workspace_id=${encodeURIComponent(workspaceId)}`, {method:"POST",body:JSON.stringify({brief_version_id:briefKey,field_label:field,value,expected_project_version:project.project_version})});
    const input = formalBriefInput(projectId);
    if (input.drafts[field]?.text === submitted) {
      delete input.drafts[field]; if (input.editingField === field) { input.editingField=""; input.text=""; }
      persistFormalBriefInput(projectId,input);
    }
    if (!isFormalBriefProject(projectId)) return;
    restoreFormalBriefInput(input);
    await loadFormalBrief(projectId);
    if (!isFormalBriefProject(projectId)) return;
    showToast("字段已保存，原来源与历史内容保留；请核对并确认需求。");
  } catch (error) { if (notice.isConnected) notice.textContent=`${formalError(error)} 输入已保留，可修正后再次保存。`; }
  finally { if (button.isConnected) button.disabled=false; }
}

function supplementFormalBriefCategory() {
  const choice = state.formalCategories.find(item => item.category_id === elements.formalProjectCategory.value);
  if (!choice || !isFormalBriefProject(formalRoute().projectId)) return;
  rememberBriefSourcePanelInteraction();
  document.querySelector("#brief-source-panel").open = true;
  formalBriefInput(state.formalBriefProjectId).mode = "conversation"; renderBriefMode(formalBriefInput(state.formalBriefProjectId));
  const text = elements.formalBriefText.value.trim();
  elements.formalBriefText.value = [text, `脚本类型: ${choice.label}`].filter(Boolean).join("\n");
  renderFormalBriefSelection();
  elements.formalBriefUpload.dataset.editFactField = "脚本类型";
  rememberFormalBriefInput();formalBriefInput(state.formalBriefProjectId).revision += 1;
  elements.formalBriefUploadButton.textContent = "保存并重新分析";
  elements.formalBriefText.scrollIntoView({block:"center",behavior:"smooth"});
  elements.formalBriefText.focus({preventScroll:true});
  showToast("类型已补入，保存后核对冲突并确认需求。");
}

function renderFormalBriefSelection() {
  const files = [...elements.formalBriefFiles.files];
  const directText = briefIntakeText();
  const limits = state.formalBriefUploadLimits;
  const textBytes = new TextEncoder().encode(directText).length;
  elements.formalBriefSelection.textContent = files.length
    ? `已选择 ${files.length} 个文件：${files.map((file) => file.name).join("、")}`
    : "尚未选择文件";
  elements.formalBriefClearFiles.hidden = !files.length;
  elements.formalBriefClearFiles.disabled = !files.length || Boolean(state.formalBriefProjectId && formalBriefInput(state.formalBriefProjectId).busy);
  if (elements.formalBriefTextCount) {
    const nearLimit = limits && textBytes >= Math.min(limits.max_text_bytes, limits.max_file_bytes) * 0.8;
    elements.formalBriefTextCount.textContent = limits
      ? `${[...directText].length.toLocaleString()} 字 · 本次 ${files.length + (directText ? 1 : 0)} / ${limits.max_documents} 份资料${nearLimit ? ` · 文字容量 ${Math.round(textBytes / Math.min(limits.max_text_bytes, limits.max_file_bytes) * 100)}%，接近上限时请拆分导入` : ""}`
      : `${[...directText].length.toLocaleString()} 字`;
  }
  if (elements.formalBriefLimits) {
    const fileSize = limits?.max_file_bytes >= 1024 * 1024
      ? `${limits.max_file_bytes / (1024 * 1024)}MiB`
      : `${limits?.max_file_bytes} 字节`;
    elements.formalBriefLimits.textContent = limits
      ? `每份文件最多 ${fileSize}，每批最多 ${limits.max_documents} 份；粘贴文字计一份。`
      : "正在读取当前上传限制…";
  }
}

function formalBriefUploadProblem(files, directText, limits) {
  // Server limits are authoritative; the API still validates clients that have
  // not yet received the current DTO (including older clients).
  if (!limits) return "";
  if (files.length + (directText ? 1 : 0) > limits.max_documents) {
    return `每批最多 ${limits.max_documents} 份资料，粘贴文字计一份。`;
  }
  const oversized = files.find(file => file.size > limits.max_file_bytes);
  if (oversized) return `${oversized.name} 超过当前单文件上传上限，请压缩或拆分后上传。`;
  if (new TextEncoder().encode(directText).length > Math.min(limits.max_text_bytes, limits.max_file_bytes)) {
    return `粘贴文字超过 ${Math.min(limits.max_text_bytes, limits.max_file_bytes).toLocaleString()} 字节（UTF-8），请拆分后导入。`;
  }
  return "";
}

function installFormalBriefDropzone() {
  const zone = elements.formalBriefDropzone;
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    zone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.remove("is-dragging");
  }));
  zone.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files?.length) return;
    const transfer = new DataTransfer();
    [...event.dataTransfer.files].forEach((file) => transfer.items.add(file));
    elements.formalBriefFiles.files = transfer.files;
    rememberFormalBriefInput();
    formalBriefInput(state.formalBriefProjectId).revision += 1;
    renderFormalBriefSelection();
  });
  elements.formalBriefFiles.addEventListener("change", renderFormalBriefSelection);
  elements.formalBriefClearFiles.addEventListener("click", () => {
    if (!state.formalBriefProjectId || formalBriefInput(state.formalBriefProjectId).busy) return;
    elements.formalBriefFiles.value = "";
    rememberFormalBriefInput();
    formalBriefInput(state.formalBriefProjectId).revision += 1;
    renderFormalBriefSelection();
  });
  elements.formalBriefText.addEventListener("input", renderFormalBriefSelection);
}

async function resolveFormalConflict(conflictId, factId, button) {
  button.disabled = true;
  const projectId = formalRoute().projectId;
  const briefId = formalBriefId(projectId);
  if (!briefId) return;
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    const path = `/api/formal/projects/${encodeURIComponent(projectId)}/brief/conflicts/${encodeURIComponent(conflictId)}/resolve?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
    await api(path, { method: "POST", body: JSON.stringify({ brief_version_id: briefId, selected_fact_id: factId, expected_project_version: project.project_version }) });
    if (!isFormalBriefProject(projectId)) return;
    await loadFormalBrief(projectId);
    if (!isFormalBriefProject(projectId)) return;
    showToast("冲突已记录。");
  } catch (error) { if (isFormalBriefProject(projectId)) showToast(formalError(error)); }
}

async function confirmFormalBrief() {
  const projectId = formalRoute().projectId;
  const briefId = formalBriefId(projectId);
  if (!briefId) return;
  const button = elements.formalBriefConfirm; button.disabled = true; button.textContent = "正在确认…";
  try {
    const project = await api(`/api/formal/projects/${encodeURIComponent(projectId)}`);
    const path = `/api/formal/projects/${encodeURIComponent(projectId)}/brief/confirm?workspace_id=${encodeURIComponent(state.agentWorkspaceId)}`;
    await api(path, { method: "POST", body: JSON.stringify({ brief_version_id: briefId, expected_project_version: project.project_version }) });
    if (!isFormalBriefProject(projectId)) return;
    await loadFormalBrief(projectId);
    if (isFormalBriefProject(projectId)) showToast("Brief 已确认。");
  } catch (error) { if (isFormalBriefProject(projectId)) showToast(formalError(error)); }
  finally { if (isFormalBriefProject(projectId)) button.textContent = "确认需求"; }
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { elements.toast.hidden = true; }, 3200);
}

function uiInputValue(input) {
  if (input.type === "file") return [...(input.files || [])].map(file => `${file.name}:${file.size}:${file.lastModified}`).join("|");
  if (["checkbox", "radio"].includes(input.type)) return Boolean(input.checked);
  return input.value || "";
}

function rememberUiInput(input, before = false) {
  if (!input?.matches?.("input, textarea, select") || input.matches('[type="search"], [readonly], [data-script-field], [data-image-shot-query], [data-library-search], #formal-project-search, #agent-project-select, #formal-brief-text, #formal-brief-files, #agent-message, #formal-project-name, #formal-task-filters input, #formal-task-filters select')) return;
  if (state.uiInputBaselines.has(input)) return;
  state.uiInputBaselines.set(input, before ? uiInputValue(input) : ["checkbox", "radio"].includes(input.type) ? Boolean(input.defaultChecked) : input.defaultValue || "");
}

function hasPendingUiInputs() {
  if (formalScriptHasPendingWrites() || state.formalImageDirty || state.formalImageVersionProposal || state.formalDefaultImageProposal || state.formalScriptRevisionProposal || state.pendingPlanConflictAction || state.formalTemplateBusy || state.formalImageBusy || state.formalImageAutoBusy || state.formalImageVersionBusy || state.formalDefaultImageBusy || state.formalReviewBusy || state.formalPlanBusy || state.agentBusyActions.size) return true;
  if (elements.formalBriefText.value?.trim() || elements.formalBriefFiles.files?.length || elements.agentMessage.value?.trim() || elements.formalProjectName.value?.trim()) return true;
  if (state.formalDefaultImageSelections.size || state.formalDefaultLibrarySelected.size) return true;
  if ([...captureFormalReviewNotes().values.values()].some(value => value.trim()) || elements.formalReviewIssues.querySelectorAll("[data-review-select]:checked").length) return true;
  if ([...state.formalBriefInputs.values()].some(input => input.text?.trim() || input.files?.length || input.busy) || [...state.agentInputDrafts.values()].some(value => typeof value === "string" ? value.trim() : value?.text?.trim())) return true;
  for (const [input, value] of state.uiInputBaselines) {
    if (!input.isConnected) { state.uiInputBaselines.delete(input); continue; }
    if (uiInputValue(input) !== value) return true;
  }
  return false;
}

async function checkUiBuild() {
  if (!state.uiBuildId || state.uiBuildId === "__MPLUS_UI_BUILD__" || state.uiBuildCheckBusy) return;
  state.uiBuildCheckBusy = true;
  try {
    const result = await api("/api/health", {cache:"no-store"});
    if (!result.ui_build_id || result.ui_build_id === state.uiBuildId) return;
    state.uiUpdateAvailable = result.ui_build_id;
    elements.uiUpdateBanner.hidden = false;
    elements.uiUpdateCopy.textContent = "页面已更新";
  } catch (_error) { /* An update check must not interrupt the current workflow. */ }
  finally { state.uiBuildCheckBusy = false; }
}

function refreshUpdatedUi() {
  if (hasPendingUiInputs()) {
    elements.uiUpdateCopy.textContent = "请先保存或清除未提交的内容，再刷新。";
    showToast("还有未提交的内容，当前页面已保留。");
    return;
  }
  window.location.reload();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[character]));
}

function formatDate(timestamp) {
  if (!timestamp) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(timestamp * 1000));
}

async function bootstrapFormal() {
  state.token = "";
  state.formalSetupError = "";
  try {
    try {
      const session = await api("/api/ui/session", { cache: "no-store" });
      state.token = String(session?.token || "");
    } catch (_error) {
      state.token = "";
    }
    const setup = await api("/api/formal/setup");
    state.formalBriefAvailable = setup.brief_adapter === "ready";
    state.formalScriptAvailable = setup.script_availability === "available";
    renderFormalOnboarding(setup);
    await Promise.all([loadFormalProjects(), loadFormalCategories()]);
    if (setup.admin_diagnostics_availability === "available") await loadFormalAdminAccess();
    else if (elements.formalAdminLink) elements.formalAdminLink.hidden = true;
    state.agentProjects = state.formalProjects.slice();
  } catch (error) {
    state.formalBriefAvailable = false;
    state.formalScriptAvailable = false;
    state.formalProjects = [];
    state.formalSetupError = "项目工作区暂不可用，请稍后重试或联系管理员。";
  }
}

function bootstrapCurrentRoute() {
  return bootstrapFormal();
}

function invalidateCreativeMaterials(projectId) {
  if (formalRoute().projectId !== projectId) return;
  state.formalImages = null;
  creativeState().materialsKey = "";
}

async function openCurrentImageCreation(targetKind, targetId, prompt = "") {
  const projectId = formalRoute().projectId;
  if (!projectId || !targetId) return;
  if (targetKind === "creative_block") {
    const view = firstCreativeView(projectId);
    if (!await saveFirstCreative(view)) return;
    if (firstCreativePending(view)) { view.error = "请等待当前文字保存完成后再创作图片。"; renderFirstCreative(view); return; }
  } else {
    if (!await flushFormalScriptChanges() || !await flushCreativeProgress()) return;
  }
  await window.MPlusImageCreation.open({ projectId, workspaceId: state.agentWorkspaceId,
    targetKind, targetId, prompt, onAdopt: async () => {
      invalidateCreativeMaterials(projectId);
      if (targetKind === "creative_block") await refreshFirstCreative(firstCreativeView(projectId));
      else { await loadFormalScript(projectId); await loadFormalImageWorkspace(projectId, state.formalScript?.project_version); }
    } });
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-first-image]");
  if (button) void openCurrentImageCreation("creative_block", button.dataset.firstImage);
});

elements.creativeWorkspace.addEventListener("click", (event) => {
  const panel = event.target.closest("[data-creative-panel]");
  const shot = event.target.closest("[data-creative-shot]");
  const mode = event.target.closest("[data-creative-edit-mode]");
  const tool = event.target.closest("[data-creative-open-tool]");
  if (panel) { changeCreativePanel(panel.dataset.creativePanel); return; }
  if (shot) { creativeState().editMode = "shot"; creativeState().imageDetail = formalRoute().panel === "images"; selectCreativeShot(shot.dataset.creativeShot); return; }
  if (mode) { creativeState().editMode = mode.dataset.creativeEditMode; captureCreativeInputs(); syncCreativeShotVisibility(); return; }
  if (tool?.dataset.creativeOpenTool === "assistant") { void openWorkbenchAssistant(); return; }
  if (tool) {
    if (tool.dataset.creativeOpenTool === 'assistant') document.querySelector('#creative-assistant-kind').value = tool.closest('#creative-current-image') ? 'images' : 'text';
    openCreativeTool(tool.dataset.creativeOpenTool); return;
  }
  if (event.target.closest("[data-creative-create-image]")) { void openCurrentImageCreation("formal_shot", creativeCurrentShot()?.shot_id); return; }
  if (event.target.closest("[data-creative-change-image]")) { openCreativeImagePicker(); return; }
  if (event.target.closest("[data-creative-batch-images]")) { void openCreativeBatchImages({ selected: true, mode: event.target.closest('[data-creative-batch-images]').dataset.imageBatchMode }); return; }
  if (event.target.closest("[data-creative-match-images]")) { void openCreativeBatchImages({mode: event.target.closest('[data-creative-match-images]').dataset.imageBatchMode}); return; }
  if (event.target.closest("[data-creative-open-library]")) { openCreativeLibrary(); return; }
  if (event.target.closest("[data-creative-close-materials]")) { changeCreativePanel(state.formalScript?.state === 'empty' ? 'images' : creativeState().returnPanel || "images"); return; }
  if (event.target.closest("[data-creative-save-before-images]")) { creativeState().continueImagesAfterSave = true; openCreativeTool("save"); return; }
  if (event.target.closest("[data-creative-lock-current]")) {
    const active = creativeCurrentShot();
    if (!active) return;
    const locked = (state.formalScript?.shots || []).filter(item => item.assistant_locked && item.shot_id !== active.shot_id).map(item => item.shot_id);
    if (!active.assistant_locked) locked.push(active.shot_id);
    saveFormalScriptLocks(locked);
    return;
  }
  if (event.target.closest("[data-creative-select-visible]")) {
    creativeFilteredShots().forEach(item => { if (item.shot_id) creativeState().selectedShotIds.add(item.shot_id); });
    invalidateFormalScriptProposal(); invalidateFormalImageVersionProposal(); renderCreativeOutline(); syncCreativeShotVisibility(); return;
  }
  if (event.target.closest("[data-creative-clear-selection]")) {
    creativeState().selectedShotIds.clear(); invalidateFormalScriptProposal(); invalidateFormalImageVersionProposal(); renderCreativeOutline(); syncCreativeShotVisibility(); return;
  }
  const stage = event.target.closest("[data-creative-stage]");
  if (stage) { if (["review", "delivery"].includes(stage.dataset.creativeStage)) navigateFormal(formalPath("review", creativeState().projectId)); else changeCreativePanel(stage.dataset.creativeStage); return; }
  if (event.target.closest("[data-creative-batch-toggle]")) { creativeState().batchMode = !creativeState().batchMode; renderCreativeOutline(); return; }
  if (event.target.closest("[data-progress-conflict-read]")) { resolveCreativeProgressConflict("read"); return; }
  if (event.target.closest("[data-progress-conflict-local]")) { resolveCreativeProgressConflict("local"); return; }
  if (event.target.closest("[data-progress-conflict-server]")) { resolveCreativeProgressConflict("server"); return; }
  if (event.target.closest("#creative-save-progress")) { captureFormalImageInputs(); flushCreativeProgress({ manual: true }); return; }
  const next = event.target.closest("#creative-next-action");
  if (next) {
    event.preventDefault();
    if (next.dataset.action === "script") { creativeState().continueImagesAfterSave = true; openCreativeTool("save"); }
    else if (next.dataset.action === "images") {
      if (state.formalImageVersionBusy) return;
      changeCreativePanel("images").then(() => {
        const confirm = elements.formalImageVersionProposal.querySelector("[data-confirm-image-version]");
        if (state.formalImageVersionProposal && confirm) {
          confirm.scrollIntoView({ block: "nearest" });
          confirm.focus({ preventScroll: true });
        } else saveFormalImageVersion({ preventDefault() {} });
      });
    }
    else if (next.dataset.action === "pending") navigateFormal(next.getAttribute("href"));
    else navigateFormal(next.getAttribute("href"));
  }
});
elements.creativeWorkspace.addEventListener("toggle", event => {
  syncCreativePrimaryAction();
  if (event.target.id === "creative-assistant" && event.target.open && formalSurface() === "script") {
    syncCreativeAssistantCopy();
    void restoreImageBatch(formalRoute().projectId);
  }
}, true);
elements.creativeWorkspace.addEventListener("change", (event) => {
  const select = event.target.closest("[data-creative-select-shot]");
  if (select) {
    if (select.checked) creativeState().selectedShotIds.add(select.dataset.creativeSelectShot);
    else creativeState().selectedShotIds.delete(select.dataset.creativeSelectShot);
    invalidateFormalScriptProposal(); invalidateFormalImageVersionProposal(); renderCreativeOutline(); syncCreativeShotVisibility();
  }
});
elements.creativeShotSearch.addEventListener("input", () => { creativeState().filter = elements.creativeShotSearch.value; creativeState().page = 1; renderCreativeOutline(); renderWorkbenchCards(); });
elements.creativeAssistantScope.addEventListener("change", () => { creativeState().assistantScope = elements.creativeAssistantScope.value; invalidateFormalScriptProposal(); syncCreativeScopes(); });
document.querySelector("#creative-assistant-kind").addEventListener("change", () => { invalidateFormalScriptProposal(); syncCreativeAssistantCopy(); });
elements.formalProjectList.addEventListener("click", event => { if (event.target.closest("[data-projects-retry]")) void loadFormalProjects(); });
elements.creativeImageScope.addEventListener("change", () => { creativeState().imageNavigationScope = null; creativeState().imageMode = elements.creativeImageScope.value; if (creativeState().imageMode === "selected") { creativeState().batchMode = true; renderCreativeOutline(); } invalidateFormalImageVersionProposal(); syncCreativeShotVisibility(); validateFormalImageSelections(); });
elements.creativeWorkspace.querySelector(".creative-tabs").addEventListener("keydown", event => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...elements.creativeWorkspace.querySelectorAll("[data-creative-panel]")];
  const index = tabs.indexOf(event.target);
  if (index < 0) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].focus();
  changeCreativePanel(tabs[next].dataset.creativePanel);
});

function nextImageNavigationIndex(kind) {
  const shots = state.formalImageWorkspace?.shots || [];
  const currentIndex = shots.findIndex(shot => shot.shot_id === creativeState().shotId);
  const changed = new Set(creativeChangedImageShots().map(shot => shot.shot_id));
  const eligible = shot => kind === "pending" ? changed.has(shot.shot_id) && Boolean(formalImageDecisionIssue(shot))
    : !shot.confirmed && !shot.explicitly_changed || !shot.selected_image_key && !shot.library_candidate_key || shot.decision_kind === "temporary_text_basis";
  for (let offset = 1; offset <= shots.length; offset++) {
    const index = (currentIndex + offset + shots.length) % shots.length;
    if (eligible(shots[index])) return index;
  }
  return -1;
}

function navigateImageWithoutScopeChange(kind) {
  captureFormalImageInputs();
  const index = nextImageNavigationIndex(kind);
  if (index < 0) return;
  const current = creativeState();
  current.imageNavigationScope = current.imageNavigationScope || creativeAllowedShotIds();
  const shot = state.formalImageWorkspace.shots[index];
  selectCreativeShot(shot.shot_id, {preserveImageScope:true});
  captureCreativeInputs();
  focusFormalImageShot(index, {select:false});
  validateFormalImageSelections();
}


elements.formalOwnerConfirm.addEventListener("change", () => {
  elements.formalOwnerEnable.disabled = !elements.formalOwnerConfirm.checked;
});
elements.formalOwnerEnable.addEventListener("click", enableFormalOwner);
elements.agentProjectSelect.addEventListener("change", () => {
  const projectId = elements.agentProjectSelect.value;
  if (formalRoute().projectId && formalRoute().projectId !== projectId) navigateFormal(formalPath("assistant", projectId));
  else selectAgentProject(projectId).catch((error) => showToast(error.message));
});
elements.agentRefresh.addEventListener("click", () => {
  selectAgentProject(elements.agentProjectSelect.value).catch((error) => showToast(error.message));
});
elements.agentMessageForm.addEventListener("submit", submitAgentMessage);
elements.agentMessage.addEventListener("input", () => {
  state.agentInputDrafts.set(state.agentProjectId, elements.agentMessage.value);
  elements.agentMessageSubmit.disabled = !elements.agentMessage.value.trim() || !state.agentSessionId || state.agentBusyActions.has(`message:${state.agentProjectId}`);
});
elements.agentFeed.addEventListener("click", (event) => {
  const confirm = event.target.closest("[data-agent-confirm]");
  const dismiss = event.target.closest("[data-agent-dismiss]");
  const image = event.target.closest("[data-agent-image-select]");
  const temporary = event.target.closest("[data-agent-temporary]");
  const choice = event.target.closest("[data-agent-clarification]");
  const answer = event.target.closest("[data-agent-answer]");
  const scope = event.target.closest("[data-agent-scope-continue]");
  const retry = event.target.closest("[data-agent-preview-retry]");
  const link = event.target.closest("a[href]");
  if (choice) chooseAgentClarification(choice.dataset.agentClarification, Number(choice.dataset.agentChoiceIndex));
  else if (answer) { elements.agentMessage.focus(); elements.agentMessageHint.textContent = "填写补充说明后提交。"; }
  else if (scope) continueAgentScope(scope.dataset.agentScopeContinue);
  else if (retry) loadAgentCandidatePreviews(state.agentProjectId, state.agentEpoch);
  else if (link && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { const href = agentSafeHref(link.getAttribute("href")); if (href) { event.preventDefault(); navigateFormal(href); } }
  else if (confirm) confirmAgentAction(confirm.dataset.agentConfirm).catch((error) => showToast(error.message));
  else if (dismiss) dismissAgentAction(dismiss.dataset.agentDismiss).catch((error) => showToast(error.message));
  else if (image) selectAgentImageCandidate(image.dataset.agentImageSelect).catch((error) => showToast(error.message));
  else if (temporary) selectAgentTemporaryTextBasis(temporary.dataset.agentTemporary).catch((error) => showToast(error.message));
});
elements.versionProjectSelect.addEventListener("change", () => chooseWorkbenchProject(elements.versionProjectSelect.value));

elements.versionRefresh.addEventListener("click", loadVersionWorkbench);
elements.versionTree.addEventListener("click", (event) => {
  const button = event.target.closest("[data-version-read]");
  if (button) loadVersionPreview(button.dataset.versionRead);
  const download = event.target.closest('[data-version-workbook]');
  if (download) void downloadSavedReviewWorkbook(elements.versionProjectSelect.value, Number(download.dataset.versionWorkbook), download);
});
elements.versionCompare.addEventListener("click", compareWorkbenchVersions);
elements.versionGovernanceSelect.addEventListener("change", renderVersionLifecycle);
elements.versionLifecycle.addEventListener("click", (event) => {
  const button = event.target.closest("[data-version-action]");
  if (button) prepareVersionAction(button.dataset.versionAction);
});
elements.versionConfirmCheck.addEventListener("change", () => {
  elements.versionConfirmExecute.disabled = !elements.versionConfirmCheck.checked;
});
elements.versionConfirmExecute.addEventListener("click", executeVersionAction);
elements.versionConfirmCancel.addEventListener("click", cancelVersionConfirmation);
elements.assetProjectSelect.addEventListener("change", () => chooseWorkbenchProject(elements.assetProjectSelect.value));

elements.assetRefresh.addEventListener("click", loadAssetWorkbench);
elements.assetChoose.addEventListener("click", chooseAssetFile);
elements.assetUploadFile.addEventListener("change", () => {
  const file = elements.assetUploadFile.files[0];
  elements.assetLocalPath.value = file?.name || "";
  elements.assetFreeze.disabled = !file;
});
elements.assetFreeze.addEventListener("click", freezeAssetFile);
elements.assetFrozenList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-frozen]");
  if (!button) return;
  state.frozenAssets.splice(Number(button.dataset.removeFrozen), 1);
  renderFrozenAssets();
});
elements.assetStage.addEventListener("click", () => prepareAssetAction("create_asset_import"));
elements.assetImports.addEventListener("click", (event) => {
  const review = event.target.closest("[data-asset-review]");
  const publish = event.target.closest("[data-asset-publish]");
  const importId = review?.dataset.assetReview || publish?.dataset.assetPublish;
  const record = (state.assetContext?.imports || []).find((item) => item.import_id === importId);
  if (record) prepareAssetAction(review ? "review_asset_import" : "publish_asset_import", record);
});
elements.assetConfirmCheck.addEventListener("change", () => {
  elements.assetConfirmExecute.disabled = !elements.assetConfirmCheck.checked;
});
elements.assetConfirmExecute.addEventListener("click", executeAssetAction);
elements.assetConfirmCancel.addEventListener("click", cancelAssetConfirmation);
elements.assetSearch.addEventListener("click", searchWorkbenchAssets);
elements.assetSearchQuery.addEventListener("keydown", (event) => {
  if (event.key === "Enter") searchWorkbenchAssets();
});
elements.governanceProjectSelect.addEventListener("change", () => chooseWorkbenchProject(elements.governanceProjectSelect.value));
elements.governanceRefresh.addEventListener("click", loadGovernanceWorkbench);
elements.strategyGrid.addEventListener("click", (event) => {
  if (event.target.closest("[data-strategy-analyze]")) { void analyzeGovernanceStrategies(); return; }
  const button = event.target.closest("[data-strategy-select]");
  if (button) prepareStrategySelection(button.dataset.strategySelect);
});
elements.knowledgePropose.addEventListener("click", prepareKnowledgeCandidate);
elements.pendingKnowledge.addEventListener("click", (event) => {
  const button = event.target.closest("[data-knowledge-approve]");
  if (button) prepareKnowledgeApproval(button.dataset.knowledgeApprove);
});
elements.governanceConfirmCheck.addEventListener("change", () => {
  elements.governanceConfirmExecute.disabled = !elements.governanceConfirmCheck.checked;
});
elements.governanceConfirmExecute.addEventListener("click", executeGovernanceAction);
elements.governanceConfirmCancel.addEventListener("click", cancelGovernanceConfirmation);

document.querySelectorAll("[data-route]").forEach((link) => link.addEventListener("click", (event) => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (link.getAttribute("aria-disabled") === "true") { event.preventDefault(); return; }
  event.preventDefault();
  if (link.dataset.formalTarget) {
    navigateFormal(link.getAttribute("href"));
    return;
  }
  const target = formalNavigationTarget(link);
  navigateFormal(target);
}));
document.querySelectorAll("[data-task-view]").forEach((link) => link.addEventListener("click", (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  navigateFormal(link.getAttribute("href"));
}));
document.querySelector(".skip-link")?.addEventListener("click", (event) => {
  event.preventDefault();
  document.querySelector("#main-content")?.focus({ preventScroll: true });
  document.querySelector("#main-content")?.scrollIntoView({ block: "start" });
});
window.addEventListener("popstate", async () => {
  captureCreativeInputs();
  captureFormalImageInputs();
  syncFormalBriefProject();
  if (formalRoute().projectId !== state.formalScriptProjectId && formalScriptHasPendingWrites() && !(await flushFormalScriptChanges())) {
    window.history.pushState({}, "", formalPath("script", state.formalScriptProjectId));
    showToast("当前脚本尚未保存，已返回脚本页。请重试保存或放弃修改。");
    return;
  }
  renderFormalRoute();
});
window.addEventListener("beforeunload", (event) => {
  captureCreativeInputs();
  captureFormalImageInputs();
  if (!hasPendingUiInputs()) return;
  event.preventDefault();
  event.returnValue = "";
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && formalScriptHasPendingWrites()) {
    void flushFormalScriptChanges();
  }
  if (document.visibilityState === "visible") void checkUiBuild();
});
document.addEventListener("focusin", (event) => rememberUiInput(event.target, true));
document.addEventListener("input", (event) => rememberUiInput(event.target));
document.addEventListener("change", (event) => rememberUiInput(event.target));
elements.uiUpdateBanner.addEventListener("click", (event) => {
  if (event.target.closest("[data-ui-update-later]")) elements.uiUpdateBanner.hidden = true;
  if (event.target.closest("[data-ui-update-refresh]")) refreshUpdatedUi();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !elements.formalMobileMenu?.open) return;
  elements.formalMobileMenu.open = false;
  elements.formalMobileMenu.querySelector("summary")?.focus({ preventScroll: true });
});
elements.formalProjectCreate.addEventListener("submit", createFormalProject);
elements.formalProjectCreateCancel.addEventListener("click", () => {
  elements.formalProjectCreate.hidden = true;
  navigateFormal("/projects");
});
elements.formalProjectSearch.addEventListener("input", () => { state.formalProjectsPage = 1; state.formalArchivedPage = 1; renderFormalProjects(); });
for (const list of [elements.formalProjectList, elements.formalArchivedList]) {
  list.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-route]");
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigateFormal(link.getAttribute("href"));
  });
}
elements.formalTaskFilters.addEventListener("submit", (event) => {
  event.preventDefault();
  const createdFrom = document.getElementById("formal-task-from").value;
  const createdTo = document.getElementById("formal-task-to").value;
  if (createdFrom && createdTo && createdFrom > createdTo) {
    showToast("开始日期不能晚于结束日期，请调整日期范围后重新应用筛选。");
    document.getElementById("formal-task-to").focus();
    return;
  }
  const query = new URLSearchParams(window.location.search);
  const view = query.get("view");
  const next = new URLSearchParams();
  if (view === "runs") next.set("view", "runs");
  if (elements.formalTaskProjectFilter.value) next.set("project_id", elements.formalTaskProjectFilter.value);
  if (elements.formalTaskStatusFilter.value) next.set("status", elements.formalTaskStatusFilter.value);
  for (const [id,key] of [["formal-task-query","query"],["formal-task-from","created_from"],["formal-task-to","created_to"]]) { const value = document.getElementById(id).value.trim(); if (value) next.set(key,value); }
  navigateFormal(`/tasks${next.size ? `?${next}` : ""}`);
});
elements.formalTaskList.addEventListener("click", (event) => {
  const artifact = event.target.closest("[data-history-artifact]");
  const continueHistory = event.target.closest("[data-history-continue]");
  const confirmContinuation = event.target.closest("[data-confirm-history-continuation]");
  const confirmReview = event.target.closest("[data-confirm-history-review]");
  if (artifact) downloadHistoricalArtifact(artifact);
  if (continueHistory) previewHistoricalContinuation(continueHistory);
  if (confirmContinuation) confirmHistoricalContinuation(confirmContinuation);
  if (confirmReview) confirmHistoricalReviewImport(confirmReview);
});
elements.formalTaskList.addEventListener("keydown", (event) => {
  if (!["Enter", " "].includes(event.key)) return;
  const control = event.target.closest("[data-history-review-control]");
  if (!control) return;
  const input = control.querySelector("[data-history-review-file]");
  if (!input || input.disabled) return;
  event.preventDefault();
  input.click();
});
elements.formalTaskList.addEventListener("change", (event) => {
  const reviewFile = event.target.closest("[data-history-review-file]");
  if (reviewFile) previewHistoricalReviewImport(reviewFile);
});
elements.formalAdminRefresh?.addEventListener("click", loadFormalAdmin);
elements.formalAdminTaskList?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-admin-task]");
  if (button) loadFormalAdminTask(button.dataset.adminTask).catch((error) => showToast(formalError(error)));
});
elements.formalAdminTaskDetail?.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-admin-recovery-form]");
  if (!form) return;
  event.preventDefault();
  previewFormalAdminRecovery(form).catch((error) => showToast(formalError(error)));
});
elements.formalAdminTaskDetail?.addEventListener("change", (event) => {
  if (!event.target.matches("[data-admin-recovery-check]")) return;
  const confirm = event.target.closest("[data-admin-recovery-proposal]")?.querySelector("[data-admin-recovery-confirm]");
  if (confirm) confirm.disabled = !event.target.checked;
});
elements.formalAdminTaskDetail?.addEventListener("click", (event) => {
  if (!event.target.closest("[data-admin-recovery-confirm]")) return;
  confirmFormalAdminRecovery().catch((error) => showToast(formalError(error)));
});
elements.formalLifecycleForm.addEventListener("submit", prepareProjectLifecycle);
elements.formalLifecycleConfirmCheck.addEventListener("change", () => {
  elements.formalLifecycleExecute.disabled = !elements.formalLifecycleConfirmCheck.checked;
});
elements.formalLifecycleExecute.addEventListener("click", executeProjectLifecycle);
elements.formalLifecycleCancel.addEventListener("click", cancelProjectLifecycle);
elements.formalCategoryRetry.addEventListener("click", loadFormalCategories);
document.querySelector("#formal-brief-category-apply").addEventListener("click", supplementFormalBriefCategory);
document.querySelector("#formal-brief-category-edit").addEventListener("click", () => {
  document.querySelector("#formal-brief-category-choice").hidden = false;
  elements.formalProjectCategory.focus({preventScroll:true});
});
elements.formalTemplateRetry.addEventListener("click", () => loadFormalTemplates(formalRoute().projectId, formalSurface()));
elements.formalTemplateSelect.addEventListener("change", changeFormalTemplate);
document.querySelector('#formal-template-category').addEventListener('change', event => {
  void loadFormalTemplates(formalRoute().projectId, formalSurface(), event.target.value);
});
document.querySelector('#formal-template-category-confirm').addEventListener('click', () => {
  elements.formalProjectCategory.value = state.formalTemplates.category_id;
  document.querySelector('#brief-source-panel').open = true;
  supplementFormalBriefCategory();
});
elements.formalTemplateFile.addEventListener("change", () => {
  invalidateFormalTemplateInput();
  renderFormalTemplateControls();
});
elements.formalTemplateImport.addEventListener("click", importFormalTemplate);
elements.formalTemplateTrialButton.addEventListener("click", trialFormalTemplate);
elements.formalTemplateConfirm.addEventListener("change", renderFormalTemplateControls);
elements.formalTemplateApply.addEventListener("click", selectFormalTemplate);
elements.formalBriefUpload.addEventListener("submit", uploadFormalBrief);
elements.formalBriefFilesState.addEventListener("click", event => {
  const retry = event.target.closest("[data-brief-file-retry]");
  const replace = event.target.closest("[data-brief-file-replace]");
  if (retry) void retryFormalBriefFile(retry);
  else if (replace) replaceFormalBriefFile(replace);
});
elements.formalBriefUpload.addEventListener("input", () => {
  if (!isFormalBriefProject(formalRoute().projectId)) return;
  rememberFormalBriefInput();
  formalBriefInput(state.formalBriefProjectId).revision += 1;
});
elements.formalFactList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-edit-formal-fact]");
  if (button) beginFormalFactEdit(button);
  const close = event.target.closest("[data-inline-fact-close]");
  if (close) { const card=close.closest("article"); card.querySelector("[data-inline-fact]").remove(); card.querySelector("[data-edit-formal-fact]").hidden=false; }
  if (event.target.closest("[data-inline-fact-details]")) { rememberBriefSourcePanelInteraction(); document.querySelector("#brief-source-panel").open=true; const input=formalBriefInput(formalRoute().projectId); input.mode="details"; renderBriefMode(input); document.querySelector("#brief-detail-fields").scrollIntoView({block:"center"}); }
});
elements.formalFactList.addEventListener("input", event => {
  const form = event.target.closest("[data-inline-fact]");
  if (!form) return;
  const input = formalBriefInput(formalRoute().projectId), field=form.dataset.inlineFact;
  input.editingField=field; input.text=`${field}: ${form.querySelector("textarea").value}`;
  input.drafts[field]={...input.drafts[field],text:input.text}; input.revision+=1;
  elements.formalBriefText.value=input.text; elements.formalBriefUpload.dataset.editFactField=field;
  persistFormalBriefInput(formalRoute().projectId,input);
});
elements.formalFactList.addEventListener("submit", event => { const form=event.target.closest("[data-inline-fact]"); if (form) { event.preventDefault(); void saveInlineFormalFact(form); } });
installFormalBriefDropzone();
elements.formalBriefConfirm.addEventListener("click", confirmFormalBrief);
elements.formalBriefGateCopy.addEventListener("click", (event) => {
  const link = event.target.closest("[data-brief-return]");
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  navigateFormal(link.getAttribute("href"));
});
elements.formalImageUploadState.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-route], a[data-image-return]");
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  navigateFormal(link.getAttribute("href"));
});
elements.formalImageGrid.addEventListener("submit", submitFormalImageAnalysis);
elements.formalImageGrid.addEventListener("submit", event => {
  if (!event.target.matches("[data-library-search-form]")) return;
  event.preventDefault();
  loadFormalDefaultImages({query:event.target.querySelector("[data-library-search]").value.trim(),page:1});
});
elements.formalImageGrid.addEventListener("click", (event) => {
  const projectPreview = event.target.closest("[data-formal-project-preview-retry], [data-formal-project-preview-original]");
  if (projectPreview) {
    const index = Number(projectPreview.dataset.formalProjectPreviewRetry ?? projectPreview.dataset.formalProjectPreviewOriginal);
    const original = projectPreview.dataset.formalProjectPreviewOriginal !== undefined || projectPreview.dataset.previewOriginal === "true";
    void loadFormalImagePreviews(formalRoute().projectId, state.formalImageProjectVersion, state.formalImageEpoch,
      original ? { originalIndex: index } : { retryIndex: index });
    return;
  }
  const retry = event.target.closest("[data-default-preview-retry], [data-default-preview-original]");
  if (retry) {
    const key = retry.dataset.defaultPreviewRetry || retry.dataset.defaultPreviewOriginal;
    const candidate = [...(state.formalDefaultImages?.library_candidates || []), ...formalDefaultGalleryShots().flatMap(shot => shot.candidates || []),
      ...state.formalDefaultLibrarySelected.values(), ...state.formalDefaultImageSelections.values()].find(item => item.candidate_key === key);
    if (candidate) void loadFormalLibraryPreview(candidate, { original: Boolean(retry.dataset.defaultPreviewOriginal) || retry.dataset.previewOriginal === "true" });
    return;
  }
  if (event.target.closest("[data-library-reset]")) {
    loadFormalDefaultImages({ query: "", page: 1, matchScope: "all", filters: {} }); return;
  }
  const view = event.target.closest("[data-library-view]"), page = event.target.closest("[data-library-page]"), shot = event.target.closest("[data-library-for-shot]"), choice = event.target.closest("[data-use-library-candidate]"), remove = event.target.closest("[data-remove-library-candidate]");
  if (view) loadFormalDefaultImages({view:view.dataset.libraryView,page:1});
  else if (page) loadFormalDefaultImages({page:Number(page.dataset.libraryPage)});
  else if (shot) loadFormalDefaultImages({view:"library",shotNo:shot.dataset.libraryForShot,page:1,matchScope:"all"});
  else if (choice) useFormalLibraryCandidate(choice.dataset.useLibraryCandidate);
  else if (remove) toggleFormalLibraryCandidate(remove.dataset.removeLibraryCandidate,false);
  else if (event.target.closest("[data-remove-library-shot]")) {
    const shotNo = event.target.closest("[data-remove-library-shot]").dataset.removeLibraryShot;
    state.formalDefaultImageSelections.delete(shotNo);
    const current = state.formalDefaultImages?.shots?.find(item => String(item.shot_no) === shotNo);
    if (current) current.selected_candidate_key = "";
    invalidateFormalDefaultImageProposal(); renderFormalDefaultImages();
  }
  else if (event.target.closest("[data-upload-project-image]")) elements.formalImageUploadFiles.click();
  else if (event.target.closest("[data-load-default-images]")) loadFormalDefaultImages();
  else if (event.target.closest("[data-select-recommended-images]")) selectFormalRecommendedImages();
  else if (event.target.closest("[data-propose-default-images]")) proposeFormalDefaultImages();
  else if (event.target.closest("[data-confirm-default-images]")) confirmFormalDefaultImages();
});
elements.formalImageGrid.addEventListener("change", (event) => {
  const matchScope = event.target.closest("[data-library-match-scope]"), facet = event.target.closest("[data-library-filter]");
  if (matchScope) { loadFormalDefaultImages({ matchScope: matchScope.value, page: 1 }); return; }
  if (facet) {
    loadFormalDefaultImages({ filters: { ...state.formalDefaultImageFilters, [facet.dataset.libraryFilter]: facet.value }, page: 1 }); return;
  }
  const duplicates = event.target.closest("[data-default-image-duplicates]");
  if (duplicates) {
    const index = Number(duplicates.dataset.defaultImageDuplicates), shot = state.formalDefaultImages?.shots?.[index];
    if (shot) {shot.show_duplicate_sources=duplicates.checked;elements.formalImageGrid.querySelector(`[data-default-image-select="${index}"]`).innerHTML=formalDefaultImageOptions(shot);}
    return;
  }
  const target = event.target.closest("[data-library-shot]");
  if (target) {
    const shot = formalDefaultGalleryShots().find(item => String(item.shot_no) === target.value);
    if (shot?.shot_id) selectCreativeShot(shot.shot_id);
    creativeState().imageMode = "current";
    loadFormalDefaultImages({view:"library",shotNo:target.value,page:1});return;
  }
  const scope = event.target.closest("[data-library-scope]");
  if (scope) { creativeState().imageNavigationScope = null; creativeState().imageMode = scope.value; invalidateFormalDefaultImageProposal(); syncCreativeScopes(); renderFormalDefaultImages(); return; }
  const checkbox = event.target.closest("[data-default-library-candidate]");
  if (checkbox) {toggleFormalLibraryCandidate(checkbox.dataset.defaultLibraryCandidate,checkbox.checked);return;}
  const select = event.target.closest("[data-default-image-select]");
  if (select) selectFormalDefaultCandidate(Number(select.dataset.defaultImageSelect),select.value);
});
elements.formalImageGrid.addEventListener("toggle", event => {
  if (!event.target.matches?.("[data-default-duplicate-group]") || !event.target.open) return;
  for (const target of event.target.querySelectorAll("[data-default-preview]")) {
    const candidate = state.formalDefaultImages?.library_candidates?.find(item => item.candidate_key === target.dataset.defaultPreview);
    if (candidate) void loadFormalLibraryPreview(candidate);
  }
}, true);
window.addEventListener("storage", event => {
  if (!["mplus.agent.auth", "mplus.agent.workspace", null].includes(event.key)) return;
  state.agentAuth = localStorage.getItem("mplus.agent.auth") || "";
  state.agentWorkspaceId = localStorage.getItem("mplus.agent.workspace") || "";
  clearFormalPreviewSession();
  closeCreativeImagePicker({ restoreFocus: false });
  firstPickerClose();
});
window.addEventListener("pagehide", clearFormalPreviewSession);

elements.formalImageSynchronize.addEventListener("click", synchronizeFormalImages);
elements.formalImageUnderstand.addEventListener("click", understandFormalImages);
elements.formalImageUploadForm.addEventListener("submit", uploadFormalProjectImages);
elements.formalImageUploadFiles.addEventListener("change", () => {
  const files = [...elements.formalImageUploadFiles.files];
  elements.formalImageUploadSelection.textContent = files.length
    ? `已选择 ${files.length} 张图片：${files.map((file) => file.name).join("、")}`
    : "尚未选择图片";
});
elements.formalImageShotGrid.addEventListener("change", (event) => {
  const duplicates = event.target.closest("[data-image-shot-duplicates]");
  if (duplicates) {
    const index = Number(duplicates.dataset.imageShotDuplicates);
    const shot = state.formalImageWorkspace?.shots?.[index];
    if (shot) { shot.show_duplicate_sources = duplicates.checked; elements.formalImageShotGrid.querySelector(`[data-image-shot-select="${index}"]`).innerHTML = formalImageShotOptions(shot); }
    return;
  }
  state.formalImageDirty = true;
  if (event.target.matches("[data-image-shot-select]")) changeFormalImageSelection(event);
  else if (event.target.matches("[data-image-shot-confirm]")) validateFormalImageSelections();
  else if (event.target.matches("[data-image-shot-temporary]")) toggleFormalImageTemporary(event.target);
  else if (event.target.matches("[data-image-shot-reason]")) validateFormalImageSelections();
  else if (event.target.matches("[data-image-shot-reuse-reason]")) validateFormalImageSelections();
});
elements.formalImageShotGrid.addEventListener("input", (event) => {
  state.formalImageDirty = true;
  captureFormalImageInputs();
  if (event.target.matches("[data-image-shot-reason], [data-image-shot-reuse-reason]")) validateFormalImageSelections();
});
elements.formalImageShotGrid.addEventListener("click", (event) => {
  const use = event.target.closest("[data-image-shot-use]");
  const generate = event.target.closest("[data-image-generate]");
  if (generate) { void openCurrentImageCreation("formal_shot", generate.dataset.imageGenerate); return; }
  const picker = event.target.closest("[data-image-picker]");
  if (picker) openCreativeImagePicker("current", state.formalImageWorkspace.shots[Number(picker.dataset.imagePicker)].shot_id);
  const reuse = event.target.closest("[data-image-reuse-apply]");
  if (use) useFormalImageShot(Number(use.dataset.imageShotUse));
  if (reuse) applyFormalImageReuseReason(Number(reuse.dataset.imageReuseApply));
  const search = event.target.closest("[data-image-shot-search]");
  const supplement = event.target.closest("[data-image-supplement]");
  const link = event.target.closest("[data-image-library]");
  if (link) { event.preventDefault(); const index = Number(link.closest("[data-image-shot]").dataset.imageShot); openCreativeImagePicker("library", state.formalImageWorkspace.shots[index].shot_id); }
  if (search) searchFormalImageShot(search);
  if (supplement) supplementFormalImageShot(Number(supplement.dataset.imageSupplement));
});
elements.formalImageShotGrid.addEventListener("click", event => {
  const single = event.target.closest("[data-image-undo]");
  if (single) undoCreativeImageChoices([state.formalImageWorkspace?.shots?.[Number(single.dataset.imageUndo)]?.shot_id]);
  if (event.target.closest("[data-image-undo-selected]")) undoCreativeImageChoices([...creativeState().selectedShotIds]);
});
elements.formalImageVersionForm.addEventListener("submit", saveFormalImageVersion);
document.querySelector("#formal-image-next-incomplete")?.addEventListener("click", () => navigateImageWithoutScopeChange("pending"));
document.querySelector("#formal-image-jump")?.addEventListener("change", (event) => focusFormalImageShot(Number(event.target.value)));
elements.formalImageSupplementFile.addEventListener("change", uploadFormalImageSupplement);
elements.formalImageVersionProposal.addEventListener("click", (event) => {
  if (event.target.closest("[data-confirm-image-version]")) confirmFormalImageVersion();
});
elements.formalImageVersionMessage.addEventListener("click", (event) => {
  if (event.target.closest("[data-retry-image-version]")) saveFormalImageVersion(event);
});
elements.formalReviewStart.addEventListener("click", startFormalReview);
elements.formalReviewIssues.addEventListener("click", (event) => {
  const currentGroup = event.target.closest("[data-review-group]");
  if (currentGroup) state.formalReviewSelectedGroup = currentGroup.dataset.reviewGroup;
  const group = event.target.closest("[data-review-select-group]");
  if (group) {
    selectFormalReviewGroup(group.dataset.reviewSelectGroup);
    return;
  }
  const button = event.target.closest("[data-review-decision]");
  if (!button) return;
  const index = Number(button.dataset.reviewIndex);
  const note = elements.formalReviewIssues.querySelector(`[data-review-note="${index}"]`)?.value || "";
  if (button.dataset.reviewDecision === "return" && !note.trim()) {
    const panel = elements.formalReviewIssues.querySelector(`[data-review-note-panel="${index}"]`);
    if (panel) panel.open = true;
    showToast("请填写退回原因。");
    elements.formalReviewIssues.querySelector(`[data-review-note="${index}"]`)?.focus();
    return;
  }
  submitFormalReviewDecision(index, button.dataset.reviewDecision, note);
});
elements.formalReviewIssues.addEventListener("change", validateFormalReviewControls);
elements.formalReviewBatchApprove.addEventListener("click", submitFormalReviewBatch);
elements.formalReviewBatch.addEventListener("click", (event) => {
  if (event.target.closest("[data-review-select-current]")) {
    const group = elements.formalReviewIssues.querySelector(`[data-review-group="${state.formalReviewSelectedGroup}"]`);
    if (group?.tagName === "DETAILS") group.open = true;
    selectFormalReviewGroup();
    return;
  }
  if (!event.target.closest("[data-review-clear-selection]")) return;
  elements.formalReviewIssues.querySelectorAll("[data-review-select]:checked").forEach((input) => { input.checked = false; });
  validateFormalReviewControls();
});
elements.formalReviewFinalForm.addEventListener("submit", confirmFormalReviewFinal);
elements.formalDeliveryExcel.addEventListener("click", downloadFormalExcel);
elements.formalDeliveryPdf.addEventListener("click", downloadFormalPdf);
elements.formalPlanCreate.addEventListener("click", createFormalPlan);
document.querySelector("#formal-plan-regenerate").addEventListener("click", () => {
  if (state.formalPlanBusy) return;
  if (state.formalPlan?.creative_conversion) {
    const url = new URL(window.location.href);
    url.searchParams.set("creative", "1");
    window.history.replaceState({}, "", url.pathname + url.search);
  }
  setFormalPlanState("idle", "按当前资料重新生成规划。");
  void createFormalPlan();
});
elements.formalPlanDecisionCounts.addEventListener("click", locatePlanDecision);
elements.formalReviewDecisionCounts.addEventListener("click", locatePlanDecision);
elements.formalPlanConflictPrepare.addEventListener("click", prepareFormalPlanConflicts);
elements.formalPlanConflictList.addEventListener("change", cancelFormalPlanConflictConfirmation);
elements.formalPlanConflictCheck.addEventListener("change", () => {
  elements.formalPlanConflictExecute.disabled = !state.pendingPlanConflictAction || !elements.formalPlanConflictCheck.checked || state.formalPlanBusy;
});
elements.formalPlanConflictExecute.addEventListener("click", executeFormalPlanConflicts);
elements.formalPlanConflictCancel.addEventListener("click", cancelFormalPlanConflictConfirmation);
elements.formalPlanConfirmCheck.addEventListener("change", () => {
  elements.formalPlanConfirm.disabled = planHasDecisionGate(state.formalPlan) || !elements.formalPlanConfirmCheck.checked || state.formalPlanState !== "waiting_user" || state.formalPlanBusy;
});
elements.formalPlanConfirm.addEventListener("click", confirmFormalPlan);
elements.formalScriptTable.addEventListener("compositionstart", event => {
  const control = event.target.closest("[data-script-shot][data-script-field]");
  if (!control) return;
  state.formalScriptComposingControls.add(control);
  window.clearTimeout(state.formalScriptSaveTimer);
  state.formalScriptSaveTimer = null;
});
elements.formalScriptTable.addEventListener("compositionend", event => {
  state.formalScriptComposingControls.delete(event.target);
  queueFormalScriptCellSave(event);
});
elements.formalScriptTable.addEventListener("input", queueFormalScriptCellSave);
elements.formalScriptTable.addEventListener("change", changeFormalScriptCell);
elements.formalScriptLockEdit.addEventListener("click", editFormalScriptLocks);
elements.formalScriptLockClear.addEventListener("click", () => saveFormalScriptLocks([]));
elements.formalScriptLockSave.addEventListener("click", persistFormalScriptLocks);
elements.formalScriptLockCancel.addEventListener("click", cancelFormalScriptLocks);
elements.formalScriptQuickFeedback.addEventListener("click", (event) => {
  const button = event.target.closest("[data-script-quick-feedback]");
  if (!button) return;
  invalidateFormalScriptProposal();
  state.formalScriptFeedbackKind = button.dataset.feedbackKind || "copy";
  elements.formalScriptInstruction.value = button.dataset.scriptQuickFeedback;
  elements.formalScriptInstruction.focus({ preventScroll: true });
});
elements.formalScriptInstruction.addEventListener("input", (event) => {
  invalidateFormalScriptProposal();
  if (event.isTrusted) state.formalScriptFeedbackKind = "";
});
elements.formalScriptScopeContinue.addEventListener("click", continueFormalScriptScope);
elements.formalScriptInstructionForm.addEventListener("submit", applyFormalScriptInstruction);
function handleFormalScriptProposalClick(event) {
  if (event.target.closest("[data-script-next-save]")) {
    openCreativeTool("save");
    return;
  }
  if (event.target.closest("[data-confirm-script-revision]")) {
    confirmFormalScriptRevision();
    return;
  }
  const next = event.target.closest("[data-script-next-href]");
  if (next) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigateFormal(next.getAttribute("href"));
    return;
  }
  const choice = event.target.closest("[data-script-revision-instruction]");
  if (!choice?.dataset.scriptRevisionInstruction) return;
  invalidateFormalScriptProposal();
  elements.formalScriptInstruction.value = choice.dataset.scriptRevisionInstruction;
  elements.formalScriptInstruction.focus({ preventScroll: true });
}
elements.formalScriptRevisionProposal.addEventListener("click", handleFormalScriptProposalClick);
elements.formalScriptSaveProposal.addEventListener("click", handleFormalScriptProposalClick);
elements.formalScriptUndo.addEventListener("click", () => formalScriptHistory("undo"));
elements.formalScriptRedo.addEventListener("click", () => formalScriptHistory("redo"));
elements.formalScriptRetry.addEventListener("click", () => flushFormalScriptChanges({ allowRetry: true }));
document.querySelector("#formal-script-conflict").addEventListener("click", event => {
  for (const action of ["read", "local", "server"]) {
    if (event.target.closest(`[data-script-conflict-${action}]`)) void resolveFormalScriptSaveConflict(action);
  }
});
elements.formalScriptDiscard.addEventListener("click", discardFormalScriptChanges);
elements.formalScriptSaveForm.addEventListener("submit", saveFormalScriptVersion);
elements.formalScriptSaveReason.addEventListener("input", invalidateFormalScriptProposal);
elements.formalScriptReviewWorkbook.addEventListener("click", () => void downloadFormalScriptReviewWorkbook());
elements.formalScriptVersions.addEventListener("click", event => {
  const button = event.target.closest("[data-script-version-workbook]");
  if (button) void downloadFormalScriptReviewWorkbook(Number(button.dataset.scriptVersionWorkbook), button);
});
elements.formalScriptVersionGovernance.addEventListener("click", (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (event.currentTarget.getAttribute("aria-disabled") === "true") {
    event.preventDefault();
    return;
  }
  event.preventDefault();
  navigateFormal(event.currentTarget.getAttribute("href"));
});
elements.formalScriptVersions.addEventListener("click", (event) => {
  const button = event.target.closest("[data-script-version-diff]");
  if (!button) return;
  loadFormalScriptDiff(Number(button.dataset.scriptVersionDiff), { focus: true });
});
syncFormalBriefProject();
void checkUiBuild();
bootstrapCurrentRoute().finally(() => renderFormalRoute());

// Shared workspace controls preserve server identities across panels.
for (const eventName of ["input", "change"]) elements.formalImageGrid.addEventListener(eventName, event => {
  const form = event.target.closest("[data-formal-image-form]");
  if (form) captureFormalAnalysisForm(form);
});
elements.creativeAnalysisConfirm?.addEventListener("click", confirmCreativeImageAnalyses);
elements.creativeReviewReturnNote?.addEventListener("input", validateFormalReviewControls);
elements.creativeReviewBatchReturn?.addEventListener("click", () => {
  const indexes = [...elements.formalReviewIssues.querySelectorAll("[data-review-select]:checked")].map(input => Number(input.dataset.reviewSelect));
  submitFormalReviewDecisions(indexes, "return", elements.creativeReviewReturnNote.value);
});
document.addEventListener("change", event => {
  const analysis = event.target.closest("[data-analysis-select]");
  if (analysis) {
    const image = state.formalImages?.images?.find(item => item.image_key === analysis.dataset.analysisSelect);
    if (analysis.checked && creativeAnalysisEligible(image)) creativeState().analysisSelected.set(image.image_key, image.analysis_key);
    else creativeState().analysisSelected.delete(analysis.dataset.analysisSelect);
    renderCreativeAnalysisControls();
  }
  const retained = event.target.closest("[data-creative-retain-shot]");
  if (retained) updateCreativeProposalSelection(retained);
});
document.addEventListener("click", event => {
  if (event.target.closest("[data-analysis-select-visible]")) {
    for (const image of state.formalImages?.images || []) if (creativeAnalysisEligible(image)) creativeState().analysisSelected.set(image.image_key, image.analysis_key);
    elements.formalImageGrid.querySelectorAll("[data-analysis-select]:not(:disabled)").forEach(input => { input.checked = true; });
    renderCreativeAnalysisControls();
  }
  if (event.target.closest("[data-analysis-clear]")) {
    creativeState().analysisSelected.clear();
    elements.formalImageGrid.querySelectorAll("[data-analysis-select]").forEach(input => { input.checked = false; });
    renderCreativeAnalysisControls();
  }
});

window.addEventListener("resize", syncCreativeOutlinePosition);

// Additive workbench controls retain URL history and existing business commands.
document.querySelector("#tools-project-select").addEventListener("change", event => chooseWorkbenchProject(event.target.value));
document.querySelector("#admin-task-filters").addEventListener("submit", event => {
  event.preventDefault();
  const params = new URLSearchParams();
  for (const [key,value] of new FormData(event.currentTarget)) if (String(value).trim()) params.set(key,String(value).trim());
  navigateFormal(`/admin?${params}`);
});
document.querySelector("#admin-task-back").addEventListener("click", () => {
  state.formalAdminDetailEpoch += 1;
  document.querySelector("#admin-task-layout").classList.remove("has-selection");
  document.querySelector("#page-admin").classList.remove("has-task-detail");
  const url = new URL(window.location.href); url.searchParams.delete("task_id");
  window.history.replaceState({}, "", `${url.pathname}${url.search}`);
  elements.formalAdminTaskList.querySelector(`[data-admin-task="${CSS.escape(state.formalAdminSelected)}"]`)?.focus({preventScroll:true});
});
document.addEventListener("click", event => {
  const target = event.target;
  const link = target.closest("[data-list-page], [data-admin-section], [data-tool-link], #workbench-return");
  if (link && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
    event.preventDefault();
    if (link.getAttribute("aria-disabled") === "true") { document.querySelector("#tools-project-select").focus(); return; }
    navigateFormal(link.getAttribute("href"));
  }
  if (target.closest("[data-home-task-retry]")) loadHomeTasks();
  if (target.closest("[data-task-refresh]")) { const params = new URLSearchParams(window.location.search); params.delete("cursor"); navigateFormal(`/tasks?${params}`); }
  if (target.closest("[data-assets-retry]")) loadAssetWorkbench();
  if (target.closest("[data-admin-list-retry]")) loadFormalAdmin();
  const detail = target.closest("[data-admin-detail-retry]");
  if (detail) loadFormalAdminTask(detail.dataset.adminDetailRetry, {history:false});
  if (target.closest("[data-admin-recovery-cancel]")) {
    state.formalAdminProposal = null;
    const panel = elements.formalAdminTaskDetail.querySelector("[data-admin-recovery-proposal]");
    panel.hidden = true;
    elements.formalAdminTaskDetail.querySelector('textarea[name="reason"]')?.focus();
  }
});
elements.formalAdminTaskDetail.addEventListener("input", event => {
  if (!event.target.closest("[data-admin-recovery-form]")) return;
  state.formalAdminProposal = null;
  state.formalAdminIntent = null;
  const panel = elements.formalAdminTaskDetail.querySelector("[data-admin-recovery-proposal]");
  if (panel) panel.hidden = true;
});

async function loadAdminVerification() {
  const target = document.querySelector("#admin-provider-verification"), epoch = state.formalAdminEpoch;
  const active = () => formalRoute().page === "admin" && adminSection() === "system" && epoch === state.formalAdminEpoch;
  target.innerHTML = '<p role="status">正在读取配置及验证记录…</p>';
  try {
    const result = await api(`/api/formal/admin/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/connections`);
    if (!active()) return;
    const labels = {unverified:"未验证", unknown:"结果尚未确认", succeeded:"本次调用成功", failed:"本次调用失败", unconfigured:"未配置"};
    target.innerHTML = `<div class="connection-check-grid">${result.services.map(item => `<article class="ui00-summary-card"><h3>${escapeHtml(item.label)}</h3><p>${item.configured ? "已配置，实际可用性以测试结果为准。" : "尚未配置。"}</p><strong>${escapeHtml(labels[item.verification.status] || "未验证")}</strong><p>${escapeHtml(item.verification.summary || "")}</p>${item.verification.created_at ? `<small>${escapeHtml(taskDisplayTime(item.verification.created_at))} · ${escapeHtml(item.verification.method || "固定匿名样例")} · ${escapeHtml(item.verification.duration_ms ?? "—")} ms · ${item.verification.real_provider ? "真实外部调用" : "本地替身测试"}</small>` : ""}<p>点击会实际调用服务，可能产生费用；仅使用固定匿名样例。</p><button class="secondary-button compact" data-test-connection="${escapeHtml(item.kind)}"${result.can_verify ? "" : " disabled"}>测试连接</button>${item.verification.status === "unknown" && result.can_verify ? `<button class="text-button" data-new-connection="${escapeHtml(item.kind)}">明确发起新的测试</button><p>新测试可能再次产生费用；原结果不明的请求仍保留。</p>` : ""}${!result.can_verify ? '<p>当前账号没有连接测试权限。</p>' : ""}</article>`).join("")}</div><h3>最近调用</h3><p>${escapeHtml(result.scope)}</p>${result.recent_calls.length ? result.recent_calls.map(item => `<article class="compact-task-row"><strong>${escapeHtml(item.project_name)}</strong><span>${escapeHtml(recoveryStatusLabel(item.status))}</span><small>${escapeHtml(taskDisplayTime(item.created_at))} · ${escapeHtml(item.duration_ms ?? "—")} ms</small>${item.error_summary ? `<p>${escapeHtml(item.error_summary)}</p>` : ""}</article>`).join("") : '<p>暂无可确认归属的调用记录。</p>'}<button class="text-button" data-connections-refresh>刷新验证记录</button>`;
  } catch (error) {
    if (active()) target.innerHTML = `<p role="alert">验证记录读取失败：${escapeHtml(formalError(error))}</p><button class="secondary-button compact" data-connections-refresh>重新读取</button>`;
  }
}

async function testAdminConnection(button) {
  const kind = button.dataset.testConnection;
  const workspaceId = state.agentWorkspaceId;
  const storageKey = `mplus.connection-request.${workspaceId}.${kind}`;
  // Retain the same intent across a lost HTTP response and a browser refresh.
  const requestId = sessionStorage.getItem(storageKey) || assetIdempotency("connection");
  sessionStorage.setItem(storageKey, requestId);
  button.disabled = true;
  button.textContent = "正在测试，请等待结果…";
  try {
    const result = await api(`/api/formal/admin/workspaces/${encodeURIComponent(workspaceId)}/connections`, {
      method: "POST", body: JSON.stringify({kind, request_id: requestId}),
    });
    if (result.status !== "unknown") sessionStorage.removeItem(storageKey);
    if (formalRoute().page === "admin" && adminSection() === "system" && workspaceId === state.agentWorkspaceId) await loadAdminVerification();
  } catch (error) {
    if (button.isConnected) {
      button.textContent = "读取本次测试结果";
      button.disabled = false;
      showToast(`测试结果未读取：${formalError(error)}。再次点击将使用同一请求，不会重复调用。`);
    }
  }
}

document.addEventListener("click", event => {
  const fresh = event.target.closest("[data-new-connection]");
  if (fresh) {
    sessionStorage.removeItem(`mplus.connection-request.${state.agentWorkspaceId}.${fresh.dataset.newConnection}`);
    fresh.dataset.testConnection = fresh.dataset.newConnection;
  }
  const test = fresh || event.target.closest("[data-test-connection]");
  if (test) { event.preventDefault(); testAdminConnection(test); }
  if (event.target.closest("[data-connections-refresh]")) { event.preventDefault(); loadAdminVerification(); }
});


async function loadAdminApprovals() {
  const target = document.querySelector("#admin-approval-list"), epoch = state.formalAdminEpoch;
  const active = () => formalRoute().page === "admin" && adminSection() === "approvals" && epoch === state.formalAdminEpoch;
  target.innerHTML = '<p role="status">正在读取授权待办…</p>';
  try {
    const result = await api(`/api/formal/admin/workspaces/${encodeURIComponent(state.agentWorkspaceId)}/approvals`);
    if (!active()) return;
    target.innerHTML = `<p>${escapeHtml(result.scope)} · ${Number(result.count)} 项</p>${result.items.length ? result.items.map(item => `<article class="compact-task-row"><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.project_name)} · ${escapeHtml(item.label)} · ${escapeHtml(governanceScopeLabels[item.scope] || (item.scope === "shared" ? "同类型共享" : item.scope))}</p></div><span>${escapeHtml({pending_review:"待审核",approved:"已审核",pending:"待审核",待审核:"待审核",待审批:"待审批",待登记:"待登记"}[item.status] || "待处理")}</span>${item.can_act ? `<a class="secondary-button compact" href="${escapeHtml(item.href)}" data-workbench-approval-link>${escapeHtml(item.next_action)}</a>` : `<p>${escapeHtml(item.next_action)}</p>`}</article>`).join("") : '<div class="workbench-empty">当前授权范围暂无待办。</div>'}${(result.unavailable_sources || []).map(item => `<p role="alert">${escapeHtml(item.project_name)}：${escapeHtml(item.summary)}</p>`).join("")}<button class="text-button" data-approvals-refresh>刷新待办</button>`;
  } catch (error) {
    if (active()) target.innerHTML = `<p role="alert">审批汇总读取失败：${escapeHtml(formalError(error))}</p><button class="secondary-button compact" data-approvals-refresh>重试读取</button>`;
  }
}
document.addEventListener("click", event => {
  if (event.target.closest("[data-approvals-refresh]")) { event.preventDefault(); loadAdminApprovals(); }
  const link = event.target.closest("[data-workbench-approval-link]");
  if (link) { event.preventDefault(); navigateFormal(link.getAttribute("href")); }
});

document.addEventListener("click", event => { if (event.target.closest("[data-overview-retry]")) loadFormalOverview(formalRoute().projectId); });

async function loadScriptTaskContext(projectId, epoch) {
  const taskId = new URLSearchParams(window.location.search).get("task_id");
  const target = document.querySelector("#script-task-context");
  if (!taskId) return;
  target.hidden = false;
  target.textContent = "正在核对任务关联的版本与镜头…";
  const active = () => epoch === state.formalScriptEpoch && formalRoute().projectId === projectId && formalRoute().page === "script";
  try {
    const context = await api(`/api/formal/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/context`);
    if (!active()) return;
    target.innerHTML = `<strong>${escapeHtml(context.title)} · ${escapeHtml(agentStatusLabel(context.status))}</strong><p>${escapeHtml(context.issue || context.message)}</p>${context.issue ? `<small>${escapeHtml(context.message)}</small>` : ""}${context.shots.map(shot => `<button class="text-button" data-task-context-shot="${escapeHtml(shot.shot_id)}">查看第 ${escapeHtml(shot.shot_no)} 镜</button>`).join("")}`;
    if (context.is_current_version && context.shots.length && !new URLSearchParams(window.location.search).has("shot_id")) selectCreativeShot(context.shots[0].shot_id, {updateUrl:false});
  } catch (error) {
    if (active()) target.innerHTML = `<strong>任务关联读取失败</strong><p>${escapeHtml(formalError(error))}</p><button class="text-button" data-task-context-retry>重试读取关联</button>`;
  }
}
document.addEventListener("click", event => {
  const shot = event.target.closest("[data-task-context-shot]");
  if (shot) selectCreativeShot(shot.dataset.taskContextShot, {focus:true});
  if (event.target.closest("[data-task-context-retry]")) loadScriptTaskContext(formalRoute().projectId, state.formalScriptEpoch);
});

function renderCreativePendingIssues() {
  const pending = state.formalScript?.pending_confirmations || [];
  const shot = creativeCurrentShot();
  const currentIssues = shot ? pending.filter(item => item.shot_id ? item.shot_id === shot.shot_id : String(item.shot_no) === String(shot.shot_no)) : [];
  const row = item => {
    const target = (state.formalScript?.shots || []).find(candidate => item.shot_id ? candidate.shot_id === item.shot_id : String(candidate.shot_no) === String(item.shot_no));
    return `<li><b>${escapeHtml(item.shot_no ? `第 ${item.shot_no} 镜` : "项目")}</b><span>${escapeHtml(item.question || item.message || item.item || item.fact || "待人工核对")}</span>${target ? `<button class="text-button" type="button" data-pending-shot="${escapeHtml(target.shot_id)}">查看镜头</button>` : ""}</li>`;
  };
  elements.formalScriptPending.hidden = pending.length === 0;
  elements.formalScriptPending.innerHTML = pending.length ? `${currentIssues.length ? `<strong>本镜需核对 ${currentIssues.length} 项</strong><ul>${currentIssues.map(row).join("")}</ul>` : ""}<details><summary>全稿 ${pending.length} 项待核对</summary><ul>${pending.map(row).join("")}</ul><p>可先保存草稿，人工审核与交付前再核对这些事项。</p></details>` : "";
}

document.addEventListener("click", event => {
  const pending = event.target.closest("[data-pending-shot]");
  if (pending) selectCreativeShot(pending.dataset.pendingShot, { focus: true });
  if (event.target.closest("#formal-image-next-gap")) navigateImageWithoutScopeChange("gap");
  const include = event.target.closest("[data-image-scope-current]");
  if (include) {
    const current = creativeState();
    current.imageNavigationScope = null;
    current.imageMode = "current";
    selectCreativeShot(current.shotId);
    validateFormalImageSelections();
  }
});

function shortBusinessText(value, maxLength = 40) { const text = String(value || ""); return [...text].length > maxLength ? [...text].slice(0, maxLength).join("") + "…" : text; }

function formalBusinessFieldLabel(field) { return ({visual:"画面",sound:"声音",voiceover:"旁白",subtitle:"字幕",structure:"结构",duration_seconds:"时长",notes:"备注",image_id:"图片选择"})[field] || (/^[\u3400-\u9fff]/.test(field) ? field : "镜头内容"); }

function focusWorkbenchObject(parameter, attribute, parent) {
  const id = new URLSearchParams(window.location.search).get(parameter);
  if (!id) return;
  const identity = `${window.location.pathname}${window.location.search}:${state.workbenchEpoch}`;
  if (state.workbenchFocusIdentity === identity) return;
  const card = parent.querySelector(`[${attribute}="${CSS.escape(id)}"]`);
  if (card) { card.focus(); state.workbenchFocusIdentity = identity; }
  else parent.insertAdjacentHTML("afterbegin", '<p role="status">原待办已不在当前列表，请核对最新状态。</p>');
}

// Section links move focus only. They never unlock a step or submit a business action.
function installConfirmedPageNavigation() {
  const sections = [
    ["formal-project-dashboard", [["最近项目", "formal-project-list"], ["已归档", "formal-archived-list"], ["项目任务", "home-task-summary"]]],
    ["formal-brief-workbench", [["资料", "brief-source-panel"], ["关键信息", "formal-fact-list"], ["模板与类型", "formal-template-picker"], ["创作标准", "formal-standard-card-details"]]],
    ["formal-image-workbench", [["上传图片", "formal-image-upload-form"], ["图片核对", "formal-image-grid"]]],
    ["formal-review-workbench", [["审核准备", "formal-review-decision"], ["逐项审核", "formal-review-issues"], ["整片确认", "formal-review-final-title"], ["交付文件", "formal-delivery-card"]]],
    ["version-desk", [["版本记录", "version-tree"], ["对比版本", "version-diff"], ["查看内容", "version-preview"], ["发布管理", "version-lifecycle"]]],
    ["asset-desk", [["查找资产", "asset-search-query"], ["导入资产", "asset-local-path"], ["导入记录", "asset-imports"]]],
    ["governance-desk", [["叙事策略", "strategy-grid"], ["生效规则", "knowledge-rules"], ["提交建议", "knowledge-statement"], ["待审核", "pending-knowledge"]]],
  ];
  for (const [hostId, links] of sections) {
    const host = document.getElementById(hostId);
    if (!host) continue;
    const nav = document.createElement("nav");
    nav.className = "page-section-nav";
    nav.setAttribute("aria-label", "当前页区域");
    nav.innerHTML = links.filter(([, id]) => document.getElementById(id)).map(([label, id]) =>
      `<button class="text-button" type="button" data-page-section="${id}">${label}</button>`).join("");
    host.prepend(nav);
  }
}

document.addEventListener("click", event => {
  const section = event.target.closest("[data-page-section]");
  if (section) {
    const target = document.getElementById(section.dataset.pageSection);
    if (!target || target.closest("[hidden]")) { showToast("本区内容尚未准备好，请先处理页面提示。"); return; }
    if (target.id === 'brief-source-panel') rememberBriefSourcePanelInteraction();
    let parent = target;
    while (parent && !parent.classList.contains("page")) {
      if (parent.tagName === "DETAILS") parent.open = true;
      parent = parent.parentElement;
    }
    target.scrollIntoView({ block: "start", behavior: "auto" });
    if (!target.matches("input, select, textarea, button, a")) target.tabIndex = -1;
    target.focus({ preventScroll: true });
  }
  const page = event.target.closest("[data-project-page]");
  if (page && ["formalProjectsPage", "formalArchivedPage"].includes(page.dataset.projectPage)) {
    const list = page.closest(".ui00-project-list");
    state[page.dataset.projectPage] = Number(page.dataset.page);
    renderFormalProjects();
    list.tabIndex = -1;
    list.focus({ preventScroll: true });
    list.scrollIntoView({ block: "start" });
  }
});
installConfirmedPageNavigation();

document.addEventListener("click", event => {
  const preview = event.target.closest("[data-preview-image-confirmation]");
  if (preview) void previewCreativeImageConfirmation(preview.dataset.previewImageConfirmation);
  if (event.target.closest("[data-confirm-image-progress]")) void confirmCreativeImageProgress();
});

// Preserve optional field expansion across draft redraws and view switches.
elements.formalScriptTable.addEventListener("toggle", event => {
  const detail = event.target.closest?.("[data-optional-field]");
  if (detail && creativeState().editMode !== "table") creativeOptionalFields.set(detail.dataset.optionalField, detail.open);
}, true);

// Shared workbench presentation. Business edits continue through the existing
// draft, image-workspace and agent APIs; cards only project their current data.
function workbenchModel() {
  const creative = state.formalScript?.state === 'empty';
  const view = creative ? firstCreativeView() : creativeState();
  const items = creative ? (view.data?.blocks || []) : (state.formalScript?.shots || []);
  const selected = creative ? view.selected : view.selectedShotIds;
  const existingIds = new Set(items.map(item => creative ? item.block_id : item.shot_id));
  if (creative ? Boolean(view.data) : Boolean(state.formalScript)) for (const key of selected) if (!existingIds.has(key)) selected.delete(key);
  const query = (creative ? view.search : view.filter || '').trim().toLocaleLowerCase();
  const id = item => creative ? item.block_id : item.shot_id;
  const value = (item, field) => creative ? view.dirty.get(`${id(item)}:${field}`)?.value ?? item.fields?.[field] ?? ''
    : state.formalScriptDirtyCells.get(`${id(item)}:${field}`)?.value ?? item[field] ?? '';
  const filtered = items.filter(item => !query || `${item.shot_no} ${creative ? Object.values(item.fields || {}).join(' ') : [item.structure, value(item, 'visual'), value(item, 'sound')].join(' ')}`.toLocaleLowerCase().includes(query));
  const pages = Math.max(1, Math.ceil(filtered.length / 6));
  view.page = Math.min(Math.max(1, view.page || 1), pages);
  return { creative, view, items, selected, filtered, id, value, pages, visible: filtered.slice((view.page - 1) * 6, view.page * 6) };
}

function renderWorkbenchCards() {
  if (formalRoute().page !== 'script') return;
  const model = workbenchModel(), {creative, view, items, selected, filtered, id, value, pages, visible} = model;
  const images = formalRoute().panel === 'images';
  document.querySelector('#creative-first-paper-title').textContent = images ? '参考图' : '脚本正文';
  document.querySelector('#creative-first-image-copy').hidden = !images;
  document.querySelector('.creative-first-navigation').hidden = images;
  document.querySelector('.creative-first-context').hidden = images;
  document.querySelector('.creative-first-actions').hidden = images;
  const firstHost = document.querySelector('#creative-first-overview');
  const textHost = document.querySelector('#creative-script-overview');
  const imageHost = document.querySelector('#creative-image-overview');
  const host = creative ? firstHost : images ? imageHost : textHost;
  for (const inactive of [firstHost, textHost, imageHost]) if (inactive !== host) inactive.replaceChildren();
  firstHost.hidden = !creative || view.editMode !== 'overview' && !images;
  textHost.hidden = creative || images || view.editMode !== 'overview';
  imageHost.hidden = creative || !images || Boolean(view.imageDetail);
  document.querySelector('#creative-first-blocks').hidden = creative && (images || view.editMode === 'overview');
  document.querySelector('.creative-edit-surface').hidden = !creative && view.editMode === 'overview';
  elements.formalImageShotGrid.hidden = !creative && images && !view.imageDetail;
  const imageHeading = document.querySelector("#workbench-image-heading");
  imageHeading.hidden = creative || !images;
  imageHeading.querySelector("button").hidden = !view.imageDetail;
  if (host.hidden) { syncWorkbenchAssistantContext(); return; }
  const noun = creative ? '段' : '镜';
  const outside = [...selected].filter(key => !filtered.some(item => id(item) === key)).length;
  const toolbar = `<div class="workbench-card-toolbar"><span>${filtered.length} ${noun}${filtered.length !== items.length ? ` / 全部 ${items.length}` : ''} · 每页 6 ${noun}</span><div><button type="button" class="text-button" data-workbench-select="page">选择本页</button><button type="button" class="text-button" data-workbench-select="filtered">选择筛选结果</button><button type="button" class="text-button" data-workbench-select="clear"${selected.size ? '' : ' disabled'}>清空</button></div></div>`;
  const cards = visible.map(item => {
    const key = id(item), no = item.shot_no;
    const title = value(item, creative ? '段落' : 'structure') || `${noun} ${no}`;
    const visual = value(item, creative ? '画面描述' : 'visual');
    const audio = value(item, creative ? '声音' : 'sound');
    const duration = value(item, creative ? '时长' : 'duration_seconds');
    const refs = creative ? (view.data?.image_references || []).filter(ref => ref.block_id === key) : [];
    const primary = refs.find(ref => ref.primary) || refs[0];
    const imageShot = !creative ? state.formalImageWorkspace?.shots?.find(shot => shot.shot_id === key) : null;
    const display = imageShot ? formalImageDisplay(imageShot) : {};
    const protectedItem = creative ? view.data?.protected_block_ids?.includes(key) : item.assistant_locked;
    const imageState = creative ? refs.length ? `${refs.length} 张参考图` : '待配参考图' : creativeImageStatus(imageShot);
    const imageMarkup = `<div class="workbench-card-image"${primary ? ` data-workbench-reference="${escapeHtml(primary.reference_id || primary.task_id)}"` : ''}>${display.url ? `<img src="${escapeHtml(display.url)}" alt="第 ${escapeHtml(no)} 镜配图" loading="lazy">` : `<span>${escapeHtml(primary ? '正在读取参考图…' : images ? '尚未选用图片' : '待配图')}</span>`}</div>`;
    return `<article class="workbench-card${selected.has(key) ? ' is-selected' : ''}${images ? ' is-image-card' : ''}" data-workbench-card="${escapeHtml(key)}"><header><label><input type="checkbox" data-workbench-select-item="${escapeHtml(key)}" aria-label="选择第 ${escapeHtml(no)} ${noun}"${selected.has(key) ? ' checked' : ''}><span>${String(no).padStart(2, '0')}</span></label><small>${escapeHtml(duration || '时长待定')}${duration && !String(duration).includes('秒') ? ' 秒' : ''}</small>${protectedItem ? '<span class="workbench-protected">已保护</span>' : ''}</header>${images ? imageMarkup : ''}<div class="workbench-card-content"><h3>${escapeHtml(title)}</h3><p class="workbench-card-visual">${escapeHtml(visual || '画面描述待补充')}</p>${!images ? `<p class="workbench-card-audio"><span>声音</span>${escapeHtml(audio || '待补充')}</p>` : ''}</div><footer><span>${escapeHtml(imageState)}</span><div><button type="button" class="text-button" data-workbench-open="${escapeHtml(key)}">${images && !creative ? '核对配图' : '编辑'}</button>${creative ? `<button type="button" class="secondary-button compact" data-workbench-first-picker="${escapeHtml(key)}">${refs.length ? '调整参考图' : '选图'}</button>` : images ? `<button type="button" class="secondary-button compact" data-workbench-picker="${escapeHtml(key)}">选图</button>` : ''}</div></footer></article>`;
  }).join('');
  host.innerHTML = toolbar + `<div class="workbench-card-grid">${cards || '<div class="workbench-empty">没有匹配内容，调整筛选即可。</div>'}</div><nav class="workbench-pagination" aria-label="${images ? '配图' : '脚本'}卡片分页"><button type="button" class="secondary-button compact" data-workbench-page="-1"${view.page <= 1 ? ' disabled' : ''}>上一页</button><span>第 ${view.page} / ${pages} 页</span><button type="button" class="secondary-button compact" data-workbench-page="1"${view.page >= pages ? ' disabled' : ''}>下一页</button></nav><div class="workbench-bulkbar"${selected.size ? '' : ' hidden'}><strong>已选 ${selected.size} ${noun}${outside ? ` · 筛选外 ${outside}` : ''}</strong><button type="button" class="primary-button compact" data-workbench-bulk="assistant">助手修改</button><button type="button" class="secondary-button compact" data-workbench-bulk="images">推荐配图</button><button type="button" class="text-button" data-workbench-bulk="protect">保护所选</button><button type="button" class="text-button" data-workbench-bulk="unprotect">解除保护</button></div>`;
  if (creative) for (const node of host.querySelectorAll('[data-workbench-reference]')) {
    const ref = view.data.image_references.find(item => (item.reference_id || item.task_id) === node.dataset.workbenchReference);
    if (ref?.preview_url) void requestFormalPreview(ref.preview_url, {owner: `creative-first:${view.projectId}`, priority: 1}).then(url => {
      if (!node.isConnected) return; const image = document.createElement('img'); image.src = url; image.alt = ref.origin_label || '初稿参考图'; node.replaceChildren(image);
    }).catch(() => { if (node.isConnected) node.textContent = '参考图暂时无法读取'; });
  }
  if (creative && view.data) { try { sessionStorage.setItem(`mplus.first.view.${state.agentWorkspaceId}:${view.projectId}`, JSON.stringify({activeBlockId:view.activeBlockId, editMode:view.editMode, search:view.search, page:view.page, selected:[...selected]})); } catch (_) {} }
  syncWorkbenchAssistantContext();
}

async function workbenchProtect(protect) {
  const {creative, view, selected} = workbenchModel();
  if (!selected.size) return;
  if (!creative) {
    const locked = new Set((state.formalScript?.shots || []).filter(shot => shot.assistant_locked).map(shot => shot.shot_id));
    for (const id of selected) protect ? locked.add(id) : locked.delete(id);
    await saveFormalScriptLocks([...locked]); renderWorkbenchCards(); return;
  }
  if (view.busy || !await saveFirstCreative(view)) return;
  view.busy = true;
  try {
    const locked = new Set(view.data.protected_block_ids || []);
    for (const id of selected) protect ? locked.add(id) : locked.delete(id);
    acceptFirstCreative(view, await api(firstCreativePath(view, 'protection'), {method: 'POST', body: JSON.stringify({expected_project_version: view.data.project_version, expected_revision: view.data.revision, protected_block_ids: [...locked], operation_key: crypto.randomUUID()})}));
    view.error = '';
  } catch (error) { view.error = formalError(error); }
  finally { view.busy = false; view.renderKey = ''; renderFirstCreative(view); }
}

function syncWorkbenchAssistantContext() {
  const label = document.querySelector('#workbench-assistant-context-copy');
  if (!label || formalRoute().page !== 'script') return;
  const {creative, view, selected, items} = workbenchModel();
  const scope = document.querySelector('#workbench-assistant-scope').value;
  const current = creative ? items.find(item => item.block_id === view.activeBlockId) : creativeCurrentShot();
  const protectedCount = creative ? (view.data?.protected_block_ids || []).length : items.filter(item => item.assistant_locked).length;
  label.textContent = `${creative ? '初稿' : '正式稿'} · ${scope === 'all' ? `全部 ${items.length} 项` : scope === 'selected' ? `已选 ${selected.size} 项` : current ? `当前第 ${current.shot_no} ${creative ? '段' : '镜'}` : '请先选择内容'}${protectedCount ? ` · ${protectedCount} 项已保护` : ''}。文字与配图可在同一条要求中提出，采用前统一核对。`;
}

function syncWorkbenchAssistant() {
  const panel = document.querySelector('#workbench-assistant');
  const launcher = document.querySelector('#workbench-assistant-launcher');
  if (!panel || !launcher) return;
  const active = formalRoute().page === 'script';
  launcher.hidden = !active;
  const conversation = elements.agentFeed.closest('.agent-conversation');
  if (!panel._home && conversation) panel._home = {node: conversation, parent: conversation.parentNode, next: conversation.nextSibling};
  if (active && panel._home) document.querySelector('#workbench-assistant-conversation').append(panel._home.node);
  else if (panel._home) { panel._home.parent.insertBefore(panel._home.node, panel._home.next); panel.hidden = true; launcher.setAttribute('aria-expanded', 'false'); }
  document.querySelector('#creative-first-assistant')?.classList.add('workbench-legacy-assistant');
  document.querySelector('#creative-assistant')?.classList.add('workbench-legacy-assistant');
  syncWorkbenchAssistantContext();
}

async function openWorkbenchAssistant(scope) {
  syncWorkbenchAssistant();
  const panel = document.querySelector('#workbench-assistant');
  if (scope) document.querySelector('#workbench-assistant-scope').value = scope;
  panel.hidden = false;
  document.querySelector('#workbench-assistant-launcher').setAttribute('aria-expanded', 'true');
  syncWorkbenchAssistantContext();
  const projectId = formalRoute().projectId;
  if (state.agentProjectId !== projectId || !state.agentSessionId) {
    try { await selectAgentProject(projectId); }
    catch (error) { elements.agentMessageHint.textContent = formalError(error); }
  }
  elements.agentMessage.focus({preventScroll: true});
}

async function workbenchMessageContext() {
  if (formalRoute().page !== 'script') return null;
  const {creative, view, selected} = workbenchModel();
  if (creative ? !await saveFirstCreative(view) : !await flushFormalScriptChanges() || !await flushCreativeProgress()) throw new Error('请先处理当前保存提示，再提交修改。');
  const scope = document.querySelector('#workbench-assistant-scope').value;
  if (scope === 'selected' && !selected.size) throw new Error('请先勾选要修改的内容。');
  const model = creative ? view.data : state.formalScript;
  return {stage: creative ? 'creative' : 'formal', scope, current_target_id: creative ? view.activeBlockId : view.shotId, selected_target_ids: [...selected], expected_project_version: model.project_version, expected_revision: model.revision ?? model.draft_revision, expected_progress_revision: creative ? null : creativeProgressQueue()?.model?.revision ?? null};
}

document.addEventListener('change', event => {
  const checkbox = event.target.closest('[data-workbench-select-item]');
  if (checkbox) {
    const {selected, creative, view} = workbenchModel();
    checkbox.checked ? selected.add(checkbox.dataset.workbenchSelectItem) : selected.delete(checkbox.dataset.workbenchSelectItem);
    if (creative) renderFirstCreativeNavigation(view); else { renderCreativeOutline(); syncCreativeShotVisibility(); captureCreativeInputs(); }
  }
  if (event.target.id === 'workbench-assistant-scope') syncWorkbenchAssistantContext();
});

function materialsReturnTarget(params) {
  for (const key of ['block_id', 'shot_id', 'focus_shot']) if (params.get(key)) return {[key]: params.get(key)};
  return {};
}

document.addEventListener('click', async event => {
  const target = event.target.closest('[data-workbench-page],[data-workbench-select],[data-workbench-open],[data-workbench-picker],[data-workbench-first-picker],[data-workbench-image-overview],[data-workbench-bulk],[data-workbench-minimize],[data-workbench-close],#workbench-assistant-launcher,[data-creative-close-materials]');
  if (!target) return;
  if (target.matches('#workbench-assistant-launcher')) { await openWorkbenchAssistant(); return; }
  if (target.matches('[data-workbench-minimize],[data-workbench-close]')) {
    document.querySelector('#workbench-assistant').hidden = true; document.querySelector('#workbench-assistant-launcher').setAttribute('aria-expanded', 'false'); document.querySelector('#workbench-assistant-launcher').focus(); return;
  }
  if (target.matches('[data-creative-close-materials]') && formalRoute().page === 'materials') {
    const params = new URLSearchParams(location.search);
    if (params.get('return_to') === 'plan') {
      navigateFormal(formalPath('plan', formalRoute().projectId, {
        ...(params.get('mode') === 'text-only' ? {mode:'text-only'} : {}),
        ...(params.get('creative') === '1' ? {creative:'1'} : {}),
      })); return;
    }
    navigateFormal(formalPath('script', formalRoute().projectId, {panel: 'images', ...materialsReturnTarget(params)})); return;
  }
  const {creative, view, selected, filtered, visible, id} = workbenchModel();
  if (target.dataset.workbenchFirstPicker) { await openFirstCreativeImagePicker(target.dataset.workbenchFirstPicker); return; }
  if (target.hasAttribute('data-workbench-image-overview')) view.imageDetail = false;
  if (target.dataset.workbenchPage) view.page += Number(target.dataset.workbenchPage);
  if (target.dataset.workbenchSelect) {
    const action = target.dataset.workbenchSelect;
    if (action === 'clear') selected.clear();
    else for (const item of action === 'page' ? visible : filtered) selected.add(id(item));
  }
  if (target.dataset.workbenchOpen || target.dataset.workbenchPicker) {
    const key = target.dataset.workbenchOpen || target.dataset.workbenchPicker;
    view.editMode = 'shot';
    if (creative) { if (formalRoute().panel === 'images') await changeCreativePanel('script'); selectFirstCreativeBlock(view, key); }
    else { view.imageDetail = formalRoute().panel === 'images'; selectCreativeShot(key); if (target.dataset.workbenchPicker) openCreativeImagePicker(); }
  }
  const bulk = target.dataset.workbenchBulk;
  if (bulk === 'assistant') await openWorkbenchAssistant('selected');
  if (bulk === 'images') {
    await openCreativeBatchImages({selected: true, mode: 'replace_selected'});
  }
  if (bulk === 'protect' || bulk === 'unprotect') await workbenchProtect(bulk === 'protect');
  if (creative) renderFirstCreativeNavigation(view); else { renderCreativeOutline(); syncCreativeShotVisibility(); captureCreativeInputs(); }
});

document.querySelector('#workbench-assistant-handle').addEventListener('pointerdown', event => {
  if (event.target.closest('button')) return;
  const panel = document.querySelector('#workbench-assistant'), handle = event.currentTarget;
  const rect = panel.getBoundingClientRect(), start = {x: event.clientX, y: event.clientY};
  handle.setPointerCapture(event.pointerId);
  const move = next => {
    panel.style.left = `${Math.max(8, Math.min(innerWidth - rect.width - 8, rect.left + next.clientX - start.x))}px`;
    panel.style.top = `${Math.max(8, Math.min(innerHeight - 64, rect.top + next.clientY - start.y))}px`;
    panel.style.right = 'auto'; panel.style.bottom = 'auto';
  };
  const stop = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', stop); handle.removeEventListener('pointercancel', stop); };
  handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', stop); handle.addEventListener('pointercancel', stop);
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !document.querySelector('dialog[open]') && !document.querySelector('#workbench-assistant').hidden) document.querySelector('[data-workbench-close]').click();
});

function workbenchConstraintLines(constraints = {}) {
  const lines = ['preserve', 'prohibit', 'unknown'].flatMap(key => {
    const value = constraints[key];
    return (Array.isArray(value) ? value : value ? [value] : []).map(text => `${key === 'preserve' ? '保留' : key === 'prohibit' ? '不改动' : '待明确'}：${String(text)}`);
  });
  if (constraints.total_seconds != null) lines.push(`全片目标时长：${constraints.total_seconds} 秒`);
  return lines;
}

function workbenchParentReviewHtml(taskId) {
  return `<p>这条文字建议属于联合修改，请在创作助手一并核对文字、图片与关联要求。</p><button type="button" class="secondary-button compact" data-workbench-parent-open="${escapeHtml(taskId)}">在创作助手核对联合建议</button>`;
}

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-workbench-parent-open]');
  if (!button) return;
  await openWorkbenchAssistant();
  const taskId = button.dataset.workbenchParentOpen;
  const card = elements.agentFeed.querySelector(`[data-workbench-proposal="${CSS.escape(taskId)}"], [data-workbench-handoff-task="${CSS.escape(taskId)}"]`);
  card?.scrollIntoView({block:'nearest'});
});

function workbenchActionListHtml(actions = []) {
  if (!actions.length) return '';
  const labels = {revise_text:'修改文字', align_duration:'调整时长', find_images:'推荐配图', create_image:'生成新图', revise_brief:'核对需求', replan:'调整规划'};
  return `<section data-workbench-actions><strong>本次动作</strong><ol>${actions.map(action => `<li><strong>${escapeHtml(labels[action.kind] || action.label || '待核对动作')}</strong>${action.target_labels?.length ? ` · ${escapeHtml(action.target_labels.join('、'))}` : ''}<p>${escapeHtml(action.instruction || action.summary || '')}</p>${action.depends_on?.length ? '<small>完成关联动作后再继续。</small>' : ''}</li>`).join('')}</ol></section>`;
}

function imageCreationRequestButtons(response, taskId) {
  if (response.type === 'workbench_proposal' || response.undone) return '';
  const requests = response.image_creation_requests || (response.type === 'image_creation_request' ? [response] : []);
  if (!requests.length) return '';
  return `<section data-workbench-image-requests><strong>逐项核对生图要求</strong><p>打开后按当前稿准备画面要求，点击原生成按钮才会发起生图。</p>${requests.map((request, index) => {
    const key = `${taskId}:${request.action_index ?? index}:${request.target_kind}:${request.target_id}`;
    agentImageCreationRequests.set(key, {...request, ...(response.image_creation_requests ? {workbenchTaskId:taskId} : {})});
    return `<div class="ui00-inline-actions"><span>${escapeHtml(request.target_label || '当前画面')}</span><button type="button" class="secondary-button compact" data-agent-image-create="${escapeHtml(key)}">核对${escapeHtml(request.target_label || '当前画面')}生图要求</button></div>`;
  }).join('')}</section>`;
}

function workbenchHandoffs(response) {
  return (response.handoffs || (response.handoff ? [response.handoff] : [])).filter(item => ['brief', 'plan'].includes(item.kind));
}

function workbenchHandoffHref(response, handoff) {
  const href = agentSafeHref(handoff.next_href || response.next_href);
  if (!href) return '';
  const url = new URL(href, location.origin);
  return url.pathname.startsWith(`/projects/${encodeURIComponent(response.project_id)}/`)
    && url.searchParams.get('workbench_task_id') === response.task_id ? href : '';
}

function workbenchHandoffResponseHtml(response, taskId) {
  const handoffs = workbenchHandoffs(response);
  const links = handoffs.map(handoff => {
    const href = workbenchHandoffHref(response, handoff);
    return href ? `<a class="secondary-button compact" data-workbench-handoff-link href="${escapeHtml(href)}">${escapeHtml(handoff.next_label || (handoff.kind === 'brief' ? '核对需求修订' : '核对规划要求'))}</a>` : '';
  }).join('');
  return `<article class="agent-message" data-workbench-handoff-task="${escapeHtml(taskId)}"><span>待核对</span><h3>先核对需求与规划</h3><p>${escapeHtml(response.summary || '原要求已保留，请在对应入口核对后继续。')}</p>${workbenchActionListHtml(response.actions)}${response.pending_actions?.length ? '<p>其余动作仍保留，完成需求或规划核对后，再回到创作助手继续。</p>' : ''}<div class="ui00-inline-actions">${links}</div></article>`;
}

async function loadWorkbenchHandoff() {
  const briefHost = document.querySelector('#workbench-brief-handoff'), planHost = document.querySelector('#workbench-plan-handoff');
  briefHost.hidden = true; planHost.hidden = true; state.workbenchHandoff = null;
  const route = formalRoute(), params = new URLSearchParams(location.search), taskId = params.get('workbench_task_id');
  const host = route.page === 'brief' ? briefHost : route.page === 'script' && route.panel === 'structure' ? planHost : null;
  if (!host || !taskId || !route.projectId || !state.agentAuth || !state.agentWorkspaceId) return;
  const identity = {projectId:route.projectId, workspaceId:state.agentWorkspaceId, auth:state.agentAuth, location:location.pathname + location.search};
  const request = state.workbenchHandoffRequest = (state.workbenchHandoffRequest || 0) + 1;
  const active = () => state.workbenchHandoffRequest === request && identity.auth === state.agentAuth && identity.workspaceId === state.agentWorkspaceId
    && identity.location === location.pathname + location.search;
  host.hidden = false; host.innerHTML = '<p role="status">正在读取原要求…</p>';
  if (host === briefHost) document.querySelector('#brief-source-panel').open = true;
  try {
    const result = await api(`/api/agent/workbench/${encodeURIComponent(taskId)}`);
    if (!active()) return;
    const response = result.response || {}, handoffs = workbenchHandoffs(response);
    if (response.task_id !== taskId || response.project_id !== identity.projectId || response.workspace_id !== identity.workspaceId
        || !handoffs.length || handoffs.some(item => item.task_id !== taskId)) {
      host.innerHTML = `<p role="status">这条转交要求不属于当前项目或已不可用，未带入任何内容。请从当前项目助手重新打开。</p><a class="secondary-button compact" href="${escapeHtml(formalPath('script', identity.projectId))}">返回当前项目创作页</a>`;
      return;
    }
    const original = response.original_instruction || handoffs.map(item => item.instruction).filter(Boolean).join('\n');
    const constraints = workbenchConstraintLines(response.constraints || handoffs[0].constraints);
    const value = {taskId, ...identity, response, original, constraints};
    state.workbenchHandoff = value;
    const content = `<div class="panel-kicker"><span>助手转交 · 待核对</span></div><p>${escapeHtml(original).replaceAll('\n', '<br>')}</p>${constraints.length ? `<ul>${constraints.map(line => `<li>${escapeHtml(line)}</li>`).join('')}</ul>` : ''}${workbenchActionListHtml(response.actions)}${response.pending_actions?.length ? '<p>其余动作仍保留；完成本页核对后再回创作助手继续。</p>' : ''}`;
    if (host === briefHost) {
      host.innerHTML = `${content}<p>原要求尚未写入需求。加入下方输入后，可以修改，再按原流程发送、分析和确认。</p><button type="button" class="secondary-button compact" data-workbench-handoff-add>加入需求输入</button><p data-workbench-handoff-notice role="status"></p>`;
    } else {
      const href = formalPath('brief', identity.projectId, {workbench_task_id:taskId, return_to:'plan', ...(params.get('creative') ? {creative:params.get('creative')} : {})});
      host.innerHTML = `${content}<p>规划页按已确认需求生成，没有直接提交修改文字的入口。请先到需求页核对这条要求，分析并确认后再返回规划。</p><a class="secondary-button compact" data-workbench-handoff-brief href="${escapeHtml(href)}">带着原要求核对需求</a>`;
    }
  } catch (error) {
    if (active()) host.innerHTML = `<p role="status">${escapeHtml(formalError(error))}</p><button type="button" class="secondary-button compact" data-workbench-handoff-retry>重新读取原要求</button>`;
  }
}

document.addEventListener('click', event => {
  if (event.target.closest('[data-workbench-handoff-retry]')) { void loadWorkbenchHandoff(); return; }
  const button = event.target.closest('[data-workbench-handoff-add]');
  if (!button) return;
  const handoff = state.workbenchHandoff;
  if (!handoff || !isFormalBriefProject(handoff.projectId) || handoff.workspaceId !== state.agentWorkspaceId) return;
  const input = formalBriefInput(handoff.projectId);
  if (input.busy) { showToast('当前输入正在处理，请完成后再加入原要求。'); return; }
  rememberFormalBriefInput();
  const append = [handoff.original, ...handoff.constraints].filter(Boolean).join('\n');
  const base = input.editingField || input.replyQuestionId ? input.generalDraft || input.drafts['']?.text || '' : input.text;
  input.text = base.includes(append) ? base : [base, append].filter(Boolean).join('\n\n');
  input.generalDraft = input.text; input.mode = 'conversation'; input.editingField = ''; input.replyQuestionId = ''; input.revision += 1;
  persistFormalBriefInput(handoff.projectId, input); restoreFormalBriefInput(input);
  document.querySelector('#brief-source-panel').open = true;
  document.querySelector('[data-workbench-handoff-notice]').textContent = '已加入输入，已有文字、详细录入与问题回复草稿均保留。核对后点击发送。';
  elements.formalBriefText.focus({preventScroll:true});
});

function workbenchProposalState(taskId, response) {
  state.workbenchProposals ||= new Map();
  let item = state.workbenchProposals.get(taskId);
  if (!item || item.response.proposal_digest !== response.proposal_digest) {
    item = {response, selected: new Set((response.groups || []).filter(group => group.selected !== false).map(group => group.group_id)), busy: false};
    state.workbenchProposals.set(taskId, item);
  } else item.response = response;
  return item;
}

function workbenchProposalHtml(response, taskId, status) {
  const item = workbenchProposalState(taskId, response);
  const applied = response.type === 'workbench_applied';
  const running = response.type === 'workbench_progress';
  const failed = response.type === 'workbench_failed';
  const notes = workbenchConstraintLines(response.constraints);
  const groups = (response.groups || []).map(group => {
    const image = group.image;
    const candidate = image?.candidates?.find(candidate => candidate.candidate_key === image.selected_candidate_key);
    const preview = candidate?.preview_url || candidate?.thumbnail_url;
    return `<section class="workbench-proposal-group"><label><input type="checkbox" data-workbench-retain="${escapeHtml(group.group_id)}"${item.selected.has(group.group_id) ? ' checked' : ''}${response.stale || item.busy ? ' disabled' : ''}>${escapeHtml(group.label || `第 ${group.number} 项`)}</label>${(group.changes || []).map(change => `<div class="workbench-proposal-change"><strong>${escapeHtml(change.field)}</strong><del>${escapeHtml(change.before)}</del><ins>${escapeHtml(change.after)}</ins></div>`).join('')}${image ? `<div class="workbench-proposal-image">${preview ? `<div class="agent-candidate-preview" data-workbench-proposal-preview="${escapeHtml(preview)}"></div>` : ''}<p>${escapeHtml(candidate?.caption || image.reason || '暂无合适候选，保留当前配图。')}</p>${candidate?.match_basis || candidate?.reason ? `<small>${escapeHtml(candidate.match_basis || candidate.reason)}</small>` : ''}<small>${escapeHtml(candidate?.source_label || '')}</small></div>` : ''}${group.pending_actions?.length ? `<div data-workbench-group-actions><strong>生成图片需求</strong>${workbenchActionListHtml(group.pending_actions)}<p>采用后可逐项核对，仍由原生图入口发起生成。</p></div>` : ''}${group.depends_on?.length ? '<small>与关联段落一起采用，保证本次修改完整。</small>' : ''}</section>`;
  }).join('');
  return `<article class="agent-message workbench-proposal" data-workbench-proposal="${escapeHtml(taskId)}"><span>${running ? '正在处理' : failed ? '尚未完成' : applied ? '已保存' : response.stale ? '需要重新预览' : '待核对'}</span><h3>${running ? '正在准备整合建议' : failed ? '本次要求已保留' : applied ? '修改已采用' : '核对本次文字与配图'}</h3><p>${escapeHtml(response.summary || '正在读取建议…')}</p>${workbenchActionListHtml(response.actions)}${notes.length ? `<details><summary>本次约束</summary><ul>${notes.map(note => `<li>${escapeHtml(note)}</li>`).join('')}</ul></details>` : ''}${response.duration_status ? `<p>${escapeHtml(response.duration_status.summary || `预计时长 ${response.duration_status.actual_seconds ?? '待定'} 秒${response.duration_status.target_seconds ? ` / 目标 ${response.duration_status.target_seconds} 秒` : ''}`)}</p>` : ''}${groups}${response.pending_actions?.length ? '<p>其余动作已保留，采用相关修改后再核对后续要求。</p>' : ''}${response.stale ? '<p>当前稿或需求已变化。保留你的要求，重新提交即可生成最新预览。</p>' : groups ? `<button type="button" class="primary-button compact" data-workbench-confirm${item.busy || !item.selected.size ? ' disabled' : ''}>采用所选 ${item.selected.size} 组</button>` : ''}${applied && response.can_undo ? `<button type="button" class="secondary-button compact" data-workbench-undo${item.busy ? ' disabled' : ''}>撤销这次修改</button>` : ''}${imageCreationRequestButtons(response, taskId)}${agentClarificationHtml(response, taskId)}<p data-workbench-proposal-notice role="status"></p></article>`;
}

async function loadWorkbenchProposalPreviews() {
  for (const host of elements.agentFeed.querySelectorAll('[data-workbench-proposal-preview]')) {
    if (host.dataset.loaded) continue;
    host.dataset.loaded = 'true';
    try {
      const url = await requestFormalPreview(host.dataset.workbenchProposalPreview, {owner: `agent:${state.agentProjectId}:${state.agentEpoch}`, priority: 1});
      if (!host.isConnected) continue;
      const image = document.createElement('img'); image.src = url; image.alt = '建议采用的图片'; host.replaceChildren(image);
    } catch (_) { if (host.isConnected) host.textContent = '预览暂时无法读取，请核对后再采用。'; }
  }
}

async function refreshWorkbenchAfterAction(projectId) {
  if (formalRoute().projectId !== projectId || formalRoute().page !== 'script') return;
  if (state.formalScript?.state === 'empty') await refreshFirstCreative(firstCreativeView(projectId));
  else { await loadFormalScript(projectId); await loadFormalImageWorkspace(projectId, state.formalScript?.project_version); }
  creativeState().planKey = ''; creativeState().imageKey = '';
  renderWorkbenchCards();
}

elements.agentFeed.addEventListener('change', event => {
  const checkbox = event.target.closest('[data-workbench-retain]');
  if (!checkbox) return;
  const host = checkbox.closest('[data-workbench-proposal]'), item = state.workbenchProposals?.get(host.dataset.workbenchProposal);
  if (!item || item.busy || item.response.stale) return;
  const groups = item.response.groups || [], key = checkbox.dataset.workbenchRetain;
  const include = id => { if (item.selected.has(id)) return; item.selected.add(id); for (const dependency of groups.find(group => group.group_id === id)?.depends_on || []) include(dependency); };
  const exclude = id => { item.selected.delete(id); for (const group of groups) if (item.selected.has(group.group_id) && group.depends_on?.includes(id)) exclude(group.group_id); };
  checkbox.checked ? include(key) : exclude(key);
  for (const control of host.querySelectorAll('[data-workbench-retain]')) control.checked = item.selected.has(control.dataset.workbenchRetain);
  const button = host.querySelector('[data-workbench-confirm]'); button.textContent = `采用所选 ${item.selected.size} 组`; button.disabled = !item.selected.size;
});

elements.agentFeed.addEventListener('click', async event => {
  const button = event.target.closest('[data-workbench-confirm],[data-workbench-undo]');
  if (!button) return;
  const host = button.closest('[data-workbench-proposal]'), taskId = host.dataset.workbenchProposal;
  const item = state.workbenchProposals?.get(taskId), projectId = state.agentProjectId;
  if (!item || item.busy) return;
  const undo = button.hasAttribute('data-workbench-undo');
  item.busy = true; button.disabled = true;
  try {
    await api(`/api/agent/workbench/${encodeURIComponent(taskId)}/${undo ? 'undo' : 'confirm'}`, {method:'POST', body:JSON.stringify(undo ? {} : {proposal_digest:item.response.proposal_digest, retained_group_ids:[...item.selected]})});
    await refreshWorkbenchAfterAction(projectId); await loadAgentSession(projectId);
  } catch (error) { host.querySelector('[data-workbench-proposal-notice]').textContent = formalError(error); }
  finally { item.busy = false; if (button.isConnected) button.disabled = false; }
});

function renderWorkbenchStructure() {
  const surface = document.querySelector('#page-plan');
  if (!surface || formalRoute().page !== 'script' || formalRoute().panel !== 'structure') return;
  let host = document.querySelector('#workbench-current-structure');
  if (!host) { host = document.createElement('section'); host.id = 'workbench-current-structure'; surface.prepend(host); }
  const {creative, items, id, value} = workbenchModel();
  host.hidden = !creative || !items.length;
  if (host.hidden) return;
  host.innerHTML = `<div class="panel-kicker"><span>当前初稿结构</span><b>${items.length} 段</b></div><p class="field-note">按当前段落顺序核对叙事；正式结构与生成继续使用下方项目规划。</p><ol class="workbench-structure-list">${items.map(item => `<li><strong>${escapeHtml(item.shot_no)} · ${escapeHtml(value(item, '段落'))}</strong><small>${escapeHtml(value(item, '时长') || '时长待定')}</small><button type="button" class="text-button" data-workbench-structure-target="${escapeHtml(id(item))}">查看脚本</button></li>`).join('')}</ol>`;
}

document.addEventListener('click', event => {
  const target = event.target.closest('[data-workbench-structure-target]');
  if (target) navigateFormal(formalPath('script', formalRoute().projectId, {panel:'script', block_id:target.dataset.workbenchStructureTarget, draft_view:'shot'}));
});

function imageBatchTargetId(item) { return item?.target_id || item?.block_id || item?.shot_id || ''; }
function imageBatchSuggestionEligible(candidate) { return candidate?.suggestion_eligible ?? candidate?.default_eligible !== false; }
function imageBatchPresentationHost() {
  return state.formalScript?.state === 'empty' ? document.querySelector('#creative-first-image-flow')
    : formalRoute().panel === 'images' ? document.querySelector('#creative-image-flow') : elements.formalScriptRevisionProposal;
}

function scheduleWorkbenchImagePoll(result) {
  state.workbenchImageTimers ||= new Map();
  const taskId = result.task.task_id;
  clearTimeout(state.workbenchImageTimers.get(taskId));
  if (!['received','understanding','executing'].includes(result.task.status)) return;
  const projectId = result.response.project_id, workspaceId = result.response.workspace_id || state.agentWorkspaceId;
  state.workbenchImageTimers.set(taskId, setTimeout(async () => {
    if (formalRoute().projectId !== projectId || state.agentWorkspaceId !== workspaceId || formalRoute().page !== 'script') return;
    try {
      const next = await api(imageBatchPath({project_id:projectId, workspace_id:workspaceId}) + `&task_id=${encodeURIComponent(taskId)}`);
      if (formalRoute().projectId !== projectId) return;
      renderImageBatchResult(next);
    } catch (error) {
      const host = imageBatchPresentationHost();
      if (host) { const notice = document.createElement('p'); notice.setAttribute('role','status'); notice.textContent = `配图进度读取暂未完成：${formalError(error)}。点击“刷新建议”继续读取，任务记录已保留。`; host.append(notice); const retry = document.createElement('button'); retry.type='button'; retry.className='secondary-button compact'; retry.textContent='刷新建议'; retry.onclick=()=>{notice.remove();retry.remove();scheduleWorkbenchImagePoll(result);}; host.append(retry); }
    }
  }, 1200));
}

async function startWorkbenchImageBatch({selected = false, mode = 'fill_missing', automatic = false, operationKey = '', rememberRequest = null} = {}) {
  const projectId = formalRoute().projectId, workspaceId = state.agentWorkspaceId;
  const {creative, view, items, selected: selectedIds, id} = workbenchModel();
  const ids = selected || mode === 'replace_selected' ? [...selectedIds] : items.map(id);
  if (!ids.length) { showToast('请先选择需要配图的内容。'); return false; }
  if (creative ? !await saveFirstCreative(view) : !await flushFormalScriptChanges() || !await flushCreativeProgress({manual:true})) return false;
  const active = () => formalRoute().projectId === projectId && state.agentWorkspaceId === workspaceId && creative === (state.formalScript?.state === 'empty');
  if (!active()) return false;
  if (!automatic && formalRoute().panel !== 'images') await changeCreativePanel('images');
  if (!active()) return false;
  const model = creative ? view.data : state.formalScript;
  const key = operationKey || crypto.randomUUID();
  const request = {projectId, workspaceId, body: {
    expected_project_version:model.project_version, target_kind:creative ? 'creative_block' : 'formal_shot',
    ...(creative ? {selected_block_ids:ids, expected_creative_revision:model.revision} : {selected_shot_ids:ids}),
    instruction: mode === 'replace_selected' ? '重新推荐所选内容的配图，先展示建议供核对。' : '为当前稿补齐缺图，保留现有已确认选择，先展示建议供核对。',
    mode, idempotency_key:key,
  }};
  if (rememberRequest) rememberRequest(request);
  return dispatchWorkbenchImageBatch(request);
}

async function dispatchWorkbenchImageBatch(request) {
  const {projectId, workspaceId, body} = request;
  const active = () => formalRoute().page === 'script' && formalRoute().projectId === projectId && state.agentWorkspaceId === workspaceId
    && (state.formalScript?.state === 'empty' ? 'creative_block' : 'formal_shot') === body.target_kind;
  const host = imageBatchPresentationHost();
  if (active()) { host.hidden = false; host.innerHTML = '<p role="status">正在读取本次配图推荐…</p>'; }
  try {
    const result = await api(imageBatchPath({project_id:projectId, workspace_id:workspaceId}), {method:'POST', body:JSON.stringify(body)});
    if (!active()) return true;
    result.response.status = result.task.status;
    creativeState().imageFlow = result;
    const currentHost = imageBatchPresentationHost(); currentHost.hidden = false;
    currentHost.innerHTML = imageBatchProposalHtml(result.response, result.task.task_id);
    scheduleWorkbenchImagePoll(result); void loadImageBatchPreviews(currentHost);
    return true;
  } catch (error) {
    if (active()) { const currentHost = imageBatchPresentationHost(); currentHost.hidden = false; currentHost.innerHTML = `<p role="status">${escapeHtml(formalError(error))}。当前稿与已选图片保留。</p>`; }
    return false;
  }
}

function workbenchRecommendationKey() { return `mplus.workbench.recommend.${state.agentWorkspaceId}:${formalRoute().projectId}`; }
function rememberWorkbenchRecommendation(stage, generationTaskId) {
  const item = {stage, generationTaskId, operationKey:crypto.randomUUID(), status:'pending'};
  try { localStorage.setItem(workbenchRecommendationKey(), JSON.stringify(item)); } catch (_) {}
}
async function maybeRecommendWorkbenchGeneration() {
  if (formalRoute().page !== 'script') return;
  const key = workbenchRecommendationKey();
  let pending; try { pending = JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return; }
  if (!pending || state.workbenchRecommendationBusy === key) return;
  if (pending.status !== 'pending') { renderWorkbenchRecommendationRecovery(); return; }
  if (pending.stage !== (state.formalScript?.state === 'empty' ? 'creative' : 'formal')) return;
  if (pending.stage === 'creative') {
    const view = firstCreativeView();
    if (view.data?.task?.task_id !== pending.generationTaskId || view.data.task.status !== 'completed' || !view.data.blocks?.length || view.busy || firstCreativePending(view)) return;
  } else if (state.formalScript?.base_version_id !== pending.generationTaskId) return;
  await runWorkbenchRecommendation(pending, key);
}

function renderWorkbenchRecommendationRecovery() {
  document.querySelectorAll('[data-workbench-recommendation-recovery]').forEach(node => node.remove());
  if (formalRoute().page !== 'script' || !state.formalScript) return;
  const key = workbenchRecommendationKey();
  let pending; try { pending = JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return; }
  if (!pending || !['dispatching', 'retry_required'].includes(pending.status) || state.workbenchRecommendationBusy === key) return;
  const sameStage = pending.stage === (state.formalScript.state === 'empty' ? 'creative' : 'formal');
  const host = imageBatchPresentationHost(), notice = document.createElement('section');
  notice.dataset.workbenchRecommendationRecovery = '';
  notice.innerHTML = sameStage
    ? '<p role="status">上次配图推荐尚未收到完整结果。重试会继续读取同一次请求。</p><button type="button" class="secondary-button compact" data-workbench-retry-recommendation>重试本次推荐</button>'
    : '<p role="status">稿件阶段已变化，上次推荐记录已保留。可在当前配图页选择内容后重新推荐。</p>';
  host.hidden = false; host.append(notice);
}

async function runWorkbenchRecommendation(pending, key) {
  if (state.workbenchRecommendationBusy === key) return;
  state.workbenchRecommendationBusy = key;
  let success = false;
  try {
    pending.status = 'dispatching'; localStorage.setItem(key, JSON.stringify(pending));
    // Persist the exact scope and versions before dispatch so a lost response
    // can be retried through the service's existing idempotency receipt.
    success = pending.request ? await dispatchWorkbenchImageBatch(pending.request)
      : await startWorkbenchImageBatch({automatic:true, operationKey:pending.operationKey, rememberRequest: request => {
        pending.request = request; localStorage.setItem(key, JSON.stringify(pending));
      }});
    pending.status = success ? 'started' : 'retry_required'; localStorage.setItem(key, JSON.stringify(pending));
  } catch (error) { showToast(formalError(error)); }
  finally { state.workbenchRecommendationBusy = ''; renderWorkbenchRecommendationRecovery(); }
  if (key === workbenchRecommendationKey()) showToast(success ? '配图推荐已连接，结果会保留到当前项目。' : '脚本已保存。可点击“重试本次推荐”继续读取。');
}

document.addEventListener('click', event => {
  if (!event.target.closest('[data-workbench-retry-recommendation]')) return;
  const key = workbenchRecommendationKey();
  let pending; try { pending = JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return; }
  if (!pending || !['dispatching', 'retry_required'].includes(pending.status)
      || pending.stage !== (state.formalScript?.state === 'empty' ? 'creative' : 'formal')) return;
  event.target.closest('button').disabled = true;
  void runWorkbenchRecommendation(pending, key);
});

"use client";

/**
 * CrmWorkspace — client composition root of the CRM page (story 9.3,
 * FE-CRM-PAGING). The TABLE's row source is the server-paged REST listing
 * (GET /api/crm/leads, keyset cursors) through useCrmLeadsPage — no Firestore
 * get-all rows feed pagination. The realtime stream remains ONLY for the
 * live-notification wiring (deep-link selection and same-customer grouping
 * keep their contracts on the shared projection).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Card, Space, Typography } from "antd";
import { useAuth } from "@/lib/AuthProvider";
import { useRealtimeContainer } from "@/lib/realtime/useRealtimeContainer";
import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import {
  loadActiveProjects,
  projectDisplayName,
  type ActiveProject,
} from "@/features/chat/activeProjects";
import { CustomerDetail } from "./CustomerDetail";
import {
  EMPTY_LEAD_TOOLBAR_FILTER,
  selectCustomerLeadsByMaskedPhone,
  type LeadToolbarFilterCriteria,
} from "./crmLeadFilters";
import type { CrmLeadsPageFilters } from "@/domain/crm/leadPage";
import type { LeadWorkflowStatus } from "@/domain/crm/lead";
import { LeadTable } from "./LeadTable";
import { RejectedFilter } from "./RejectedFilter";
import { useSharedCrmLeadStream } from "./useCrmLeadStream";
import { useCrmLeadsPage } from "./useCrmLeadsPage";

export function CrmWorkspace() {
  const { user } = useAuth();
  const { crmNoteStore } = useRealtimeContainer();
  // Firestore rules composition: a sales may only query leads assigned to
  // their own auth uid; admins stream everything. (Notification stream scope.)
  const [activeProjects, setActiveProjects] = useState<ActiveProject[]>([]);
  const [selectedProjectKey, setSelectedProjectKey] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<LeadWorkflowStatus | null>(null);
  const [criteria, setCriteria] = useState<LeadToolbarFilterCriteria>(
    EMPTY_LEAD_TOOLBAR_FILTER
  );
  const [selectedLeadId, setSelectedLeadId] = useState<string | null>(null);
  const [queryLead, setQueryLead] = useState<string | null>(null);
  const [bearerToken, setBearerToken] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadActiveProjects().then((projects) => {
      if (!cancelled) {
        setActiveProjects(projects);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshBearerToken = useCallback(async () => {
    // A sign-out racing the mint must not crash via unhandled rejection: null
    // just disarms the drawer actions until the next mint on drawer open.
    setBearerToken(await getFreshIdToken().catch(() => null));
  }, []);

  useEffect(() => {
    const refreshId = window.setTimeout(() => void refreshBearerToken(), 0);
    return () => window.clearTimeout(refreshId);
  }, [refreshBearerToken, user?.uid]);

  // The paged listing mints its own token per request; a null return surfaces
  // as an auth error (re-login prompt) — never a silent all-data fallback.
  const getBearerToken = useCallback(async () => getFreshIdToken().catch(() => null), []);

  const projectOptions = useMemo(
    () =>
      activeProjects.map((project) => ({
        value: project.project_key,
        label: projectDisplayName(project),
      })),
    [activeProjects]
  );

  // The wire scope is the sales isolation key (one subscription for the whole
  // workspace); notification ownership lives in SalesNotificationProvider.
  const leadStream = useSharedCrmLeadStream() ?? {
    leads: [],
    connectionState: "connecting" as const,
    error: null,
    applyOptimisticLeadPatch: () => undefined,
    clearOptimisticLeadPatch: () => undefined,
  };

  // Server-side filter parameters (contract C1): the reengage window dates go
  // to the backend verbatim — the server resolves calendar days in
  // Asia/Ho_Chi_Minh; the FE never shifts them.
  const pageFilters = useMemo<CrmLeadsPageFilters>(
    () => ({
      projectKey: selectedProjectKey,
      status: statusFilter,
      reengageFromIsoDate: criteria.reengageWindowFromIsoDate,
      reengageToIsoDate: criteria.reengageWindowToIsoDate,
    }),
    [criteria, selectedProjectKey, statusFilter]
  );

  const leadsPage = useCrmLeadsPage({ filters: pageFilters, getBearerToken });

  // Feed every Firestore snapshot into the paged listing's realtime overlay:
  // the hook decides baselining/insertion/coalesced refresh (see its module
  // docblock); this effect only forwards the projection verbatim.
  const reconcileRealtimeSnapshot = leadsPage.reconcileRealtimeSnapshot;
  useEffect(() => {
    reconcileRealtimeSnapshot(leadStream.leads);
  }, [reconcileRealtimeSnapshot, leadStream.leads]);

  useEffect(() => {
    const readQueryLead = () =>
      setQueryLead(new URLSearchParams(window.location.search).get("lead"));
    readQueryLead();
    window.addEventListener("popstate", readQueryLead);
    return () => window.removeEventListener("popstate", readQueryLead);
  }, []);

  useEffect(() => {
    let nextSelectedLeadId: string | null = null;
    if (queryLead !== null && /^\d+$/.test(queryLead)) {
      const leadId = Number(queryLead);
      const matchingLead = Number.isSafeInteger(leadId)
        ? leadStream.leads.find((lead) => String(lead.leadId) === queryLead)
        : undefined;
      const owned =
        matchingLead !== undefined &&
        (user?.role !== "sales" ||
          matchingLead.assignedSalesFirebaseUid === user.uid);
      if (owned && matchingLead !== undefined) {
        nextSelectedLeadId = matchingLead.id;
      }
    }

    // Selection is genuinely two-source (URL deep-link here, row clicks in
    // openCustomerDetail), so it cannot be derived during render; syncing the
    // deep-link source in this effect preserves the deep-link contract (same
    // suppression precedent as LeadConversationPanel).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSelectedLeadId(nextSelectedLeadId);
  }, [leadStream.leads, queryLead, user]);

  const anchorLead = useMemo(
    () =>
      leadStream.leads.find((lead) => lead.id === selectedLeadId) ??
      leadsPage.rows.find((lead) => lead.id === selectedLeadId) ??
      null,
    [leadStream.leads, leadsPage.rows, selectedLeadId]
  );
  const customerLeads = useMemo(
    () =>
      anchorLead !== null
        ? selectCustomerLeadsByMaskedPhone(leadStream.leads, anchorLead)
        : [],
    [leadStream.leads, anchorLead]
  );

  async function openCustomerDetail(leadId: string): Promise<void> {
    setSelectedLeadId(leadId);
    // Actions fire the moment the drawer opens; the token must be fresh.
    await refreshBearerToken();
  }

  return (
    <div>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Text type="secondary">
          {user?.role === "sales"
            ? "Bạn đang xem các lead được gán cho mình."
            : "Bạn đang xem toàn bộ lead của các dự án."}
        </Typography.Text>
        {leadStream.error !== null ? (
          <Alert
            type="error"
            showIcon
            message="Mất kết nối dữ liệu trực tiếp"
            description={leadStream.error.message}
          />
        ) : null}
        {leadsPage.error !== null ? (
          <Alert
            type="error"
            showIcon
            data-testid="crm-leads-page-error"
            message={
              leadsPage.error.isAuthError
                ? "Phiên đăng nhập đã hết hạn"
                : "Không tải được danh sách lead"
            }
            description={
              <Space direction="vertical">
                <span>{leadsPage.error.message}</span>
                {leadsPage.error.isAuthError ? (
                  <span>
                    Vui lòng{" "}
                    <a href="/login">đăng nhập lại</a>{" "}
                    để tiếp tục xem danh sách lead.
                  </span>
                ) : (
                  <a
                    href="#retry"
                    data-testid="crm-leads-retry"
                    onClick={(event) => {
                      event.preventDefault();
                      leadsPage.retry();
                    }}
                  >
                    Thử lại
                  </a>
                )}
              </Space>
            }
          />
        ) : null}
        <Card>
          <RejectedFilter
            projectOptions={projectOptions}
            selectedProjectKey={selectedProjectKey}
            onSelectProjectKey={setSelectedProjectKey}
            statusFilter={statusFilter}
            onStatusFilterChange={setStatusFilter}
            criteria={criteria}
            onCriteriaChange={setCriteria}
            matchedLeadCount={leadsPage.rows.length}
          />
        </Card>
        <Card>
          <LeadTable
            rows={leadsPage.rows}
            connectionState={leadStream.connectionState}
            isLoading={leadsPage.isLoading}
            pageIndex={leadsPage.pageIndex}
            pageSize={leadsPage.pageSize}
            hasNextPage={leadsPage.hasNextPage}
            hasPreviousPage={leadsPage.hasPreviousPage}
            onPageChange={leadsPage.goToPage}
            onPageSizeChange={leadsPage.changePageSize}
            onOpenCustomerDetail={(leadId) => void openCustomerDetail(leadId)}
          />
        </Card>
      </Space>
      <CustomerDetail
        open={selectedLeadId !== null}
        anchorLead={anchorLead}
        customerLeads={customerLeads}
        bearerToken={bearerToken}
        currentStaffUid={user?.uid ?? null}
        noteStore={crmNoteStore}
        onOptimisticLeadPatch={leadStream.applyOptimisticLeadPatch}
        onClearOptimisticLeadPatch={leadStream.clearOptimisticLeadPatch}
        onClose={() => {
          setSelectedLeadId(null);
          if (new URLSearchParams(window.location.search).has("lead")) {
            const url = new URL(window.location.href);
            url.searchParams.delete("lead");
            window.history.replaceState({}, "", url);
          }
          setQueryLead(null);
        }}
      />
    </div>
  );
}

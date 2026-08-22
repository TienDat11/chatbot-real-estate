"use client";

/**
 * CrmWorkspace — client composition root of the CRM page (story 9.3).
 *
 * Owns the cross-component state: role-derived query scope (sales stream ONLY
 * their own leads — the rules-composed where clause), the project/status/
 * rejected filters, the selected lead, and the fresh bearer token for the CRM
 * API. All realtime data flows in through useCrmLeadStream (port-typed
 * application service); notes flow through the container's CrmNoteStorePort.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, App, Card, Space, Typography } from "antd";
import type { Lead } from "@/domain/crm/lead";
import { useAuth } from "@/lib/AuthProvider";
import { useRealtimeContainer } from "@/lib/realtime/useRealtimeContainer";
import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import {
  loadActiveProjects,
  projectDisplayName,
  type ActiveProject,
} from "@/features/chat/activeProjects";
import { showBrowserLeadNotification } from "./browserLeadNotifier";
import { CustomerDetail } from "./CustomerDetail";
import {
  EMPTY_REJECTED_LEADS_FILTER,
  filterRejectedLeads,
  selectCustomerLeadsByMaskedPhone,
  type RejectedLeadsFilterCriteria,
} from "./crmLeadFilters";
import type { RejectedLeadsCompanionCriteria } from "./RejectedFilter";
import { LeadTable } from "./LeadTable";
import { RejectedFilter } from "./RejectedFilter";
import { useCrmLeadStream } from "./useCrmLeadStream";

export function CrmWorkspace() {
  const { user } = useAuth();
  const { crmNoteStore } = useRealtimeContainer();
  const { notification } = App.useApp();

  // Firestore rules composition: a sales may only query leads assigned to
  // their own auth uid; admins stream everything.
  const assignedSalesFirebaseUidFilter = user?.role === "sales" ? user.uid : null;

  const [activeProjects, setActiveProjects] = useState<ActiveProject[]>([]);
  const [selectedProjectKey, setSelectedProjectKey] = useState<string | null>(null);
  const [rejectedOnly, setRejectedOnly] = useState(false);
  const [companionCriteria, setCompanionCriteria] =
    useState<RejectedLeadsCompanionCriteria>(EMPTY_REJECTED_LEADS_FILTER);
  const [selectedLeadId, setSelectedLeadId] = useState<string | null>(null);
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
    setBearerToken(await getFreshIdToken());
  }, []);

  useEffect(() => {
    void refreshBearerToken();
  }, [refreshBearerToken, user?.uid]);

  const projectOptions = useMemo(
    () =>
      activeProjects.map((project) => ({
        value: project.project_key,
        label: projectDisplayName(project),
      })),
    [activeProjects]
  );

  // The wire scope is the sales isolation key (one subscription for the whole
  // workspace); the project select narrows rows client-side below.
  const handleIncomingLead = useCallback(
    (lead: Lead) => {
      const customerLabel = lead.name ?? "Khách mới";
      notification.open({
        message: "Lead mới",
        description: `${customerLabel}${lead.maskedPhone !== null ? ` • ${lead.maskedPhone}` : ""} • ${lead.projectKey}`,
        duration: 6,
      });
      showBrowserLeadNotification({
        title: "Lead mới",
        body: `${customerLabel} • ${lead.projectKey}`,
      });
    },
    [notification]
  );

  const leadStream = useCrmLeadStream({
    assignedSalesFirebaseUidFilter,
    onIncomingLead: handleIncomingLead,
  });

  const rejectedCriteria: RejectedLeadsFilterCriteria = useMemo(
    () => ({ ...companionCriteria, projectKey: null }),
    [companionCriteria]
  );

  const visibleLeads = useMemo(() => {
    const projectScopedLeads =
      selectedProjectKey === null
        ? leadStream.leads
        : leadStream.leads.filter(
            (lead) => lead.projectKey === selectedProjectKey
          );
    return rejectedOnly
      ? filterRejectedLeads(projectScopedLeads, rejectedCriteria)
      : projectScopedLeads;
  }, [rejectedOnly, leadStream.leads, selectedProjectKey, rejectedCriteria]);

  const anchorLead = useMemo(
    () => leadStream.leads.find((lead) => lead.id === selectedLeadId) ?? null,
    [leadStream.leads, selectedLeadId]
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
    <main style={{ padding: 24 }}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          CRM — Quản lý lead trực tiếp
        </Typography.Title>
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
        <Card>
          <RejectedFilter
            projectOptions={projectOptions}
            selectedProjectKey={selectedProjectKey}
            onSelectProjectKey={setSelectedProjectKey}
            criteria={companionCriteria}
            onCriteriaChange={setCompanionCriteria}
            rejectedOnly={rejectedOnly}
            onRejectedOnlyChange={setRejectedOnly}
            matchedLeadCount={visibleLeads.length}
          />
        </Card>
        <Card>
          <LeadTable
            leads={visibleLeads}
            connectionState={leadStream.connectionState}
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
        onClose={() => setSelectedLeadId(null)}
      />
    </main>
  );
}

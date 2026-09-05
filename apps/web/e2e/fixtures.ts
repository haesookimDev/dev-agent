import { expect, test as base } from "@playwright/test";

export { expect };
export const test = base.extend<{ releaseWork: void; runtimeErrors: void }>({
  runtimeErrors: [async ({ page }, use) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await use();
    expect(errors, "Unhandled browser errors must fail acceptance, including hydration failures").toEqual([]);
  }, { auto: true }],
  releaseWork: [async ({ request }, use) => {
    await use();
    // This API is freshly provisioned for this serial suite, never a reused service.
    // Finish pending Mock work so the next test can use the single execution slot.
    const response = await request.get("http://127.0.0.1:18100/api/work-items");
    expect(response.ok()).toBe(true);
    const items: { id: string; status: string; assigned_worker_id: string | null }[] = await response.json();
    for (const work of items.reverse()) {
      const url = `http://127.0.0.1:18100/api/work-items/${work.id}`;
      if (work.status === "cancelled" && work.assigned_worker_id === null) {
        // Queued cancellation never acquired resources; releasing a lease would be incorrect.
        const events: { event_type: string }[] = await (await request.get(`${url}/event-log`)).json();
        expect(events.some((event) => event.event_type === "lease.released")).toBe(false);
        continue;
      }
      if (!["completed", "failed", "cancelled"].includes(work.status)) {
        await expect.poll(async () => (await (await request.get(url)).json()).status).toBe("awaiting_approval");
        const approval = await request.post(`${url}/approvals`, { data: {
          kind: "pull_request", decision: "approve", payload: {},
        } });
        expect(approval.ok()).toBe(true);
        await expect.poll(async () => (await (await request.get(url)).json()).status).toBe("completed");
      }
      await expect.poll(async () => {
        const events: { event_type: string }[] = await (await request.get(`${url}/event-log`)).json();
        return events.some((event) => event.event_type === "lease.released");
      }).toBe(true);
    }
  }, { auto: true }],
});

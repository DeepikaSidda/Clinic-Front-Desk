/*
 * DecisionsFeed behaviour (task 13.1, Req 14.1, 14.2, 14.5, 14.7, 14.8).
 *
 * Thin vanilla-JS controller for the server-rendered decisions_feed.html
 * partial. All shaping/ordering is done server-side by the pure Python
 * view-model (components/decisions_feed.py); this file only:
 *
 *   1. Hydrates the feed from the embedded DecisionsFeedView JSON, rendering
 *      cards newest-first (Req 14.1) with approve/dismiss controls (Req 14.2)
 *      and toggling the empty-state message (Req 14.7).
 *   2. Subscribes to the BFF change-event channel (WebSocket/SSE) and, on a
 *      ChangeEvent{entity:"decision", kind:"created"}, refetches the feed and
 *      inserts the new card in order (real-time add, Req 14.8).
 *   3. On approve/dismiss, removes the card optimistically and reconciles
 *      against the confirming ChangeEvent{entity:"decision", kind:"updated"};
 *      if the action fails (no confirming event / ACTION_FAILED response) the
 *      card is restored with an error indication (Req 14.5, and Req 14.6).
 *
 * The `deps` object is injected so the controller is transport-agnostic and
 * testable:
 *   - fetchOpenDecisions(): Promise<DecisionsFeedView>  // GET the rebuilt feed
 *   - resolveDecision(id, action): Promise<{outcome, error?}> // approve/dismiss
 *   - subscribe(handler): unsubscribe                    // change-event channel
 */

(function (global) {
  "use strict";

  const DECISION_ENTITY = "decision";

  function createDecisionsFeed(root, deps) {
    const listEl = root.querySelector("#decisions-feed-list");
    const emptyEl = root.querySelector("#decisions-feed-empty");
    const template = root.querySelector("#decisions-feed-card-template");

    // Cards pending confirmation of an optimistic removal, keyed by id, so we
    // can restore them if the resolving action fails (Req 14.5 reconciliation).
    const pendingRemovals = new Map();

    function renderCard(card) {
      const node = template.content.firstElementChild.cloneNode(true);
      node.dataset.decisionId = card.id;
      node.querySelectorAll("[data-field]").forEach(function (el) {
        const key = el.getAttribute("data-field");
        if (key === "error") return; // populated only on failure
        if (key in card) el.textContent = String(card[key]);
      });
      node
        .querySelector('[data-action="approve"]')
        .addEventListener("click", function () {
          resolve(card, "approve");
        });
      node
        .querySelector('[data-action="dismiss"]')
        .addEventListener("click", function () {
          resolve(card, "dismiss");
        });
      return node;
    }

    // Full re-render from a DecisionsFeedView (used on hydrate and on add).
    function render(view) {
      listEl.innerHTML = "";
      (view.cards || []).forEach(function (card) {
        listEl.appendChild(renderCard(card));
      });
      updateEmptyState(view.empty_state_message);
    }

    // Toggle the empty-state message vs. the list based on remaining cards
    // (Req 14.7). Message text comes from the server view-model.
    function updateEmptyState(message) {
      const isEmpty = listEl.children.length === 0;
      if (typeof message === "string") emptyEl.textContent = message;
      emptyEl.hidden = !isEmpty;
      listEl.hidden = isEmpty;
    }

    function removeCardEl(id) {
      const el = listEl.querySelector('[data-decision-id="' + cssEscape(id) + '"]');
      if (el) el.remove();
      updateEmptyState();
      return el;
    }

    // Optimistic approve/dismiss (Req 14.5): drop the card now, confirm later.
    function resolve(card, action) {
      const el = removeCardEl(card.id);
      pendingRemovals.set(card.id, { card: card, el: el });
      Promise.resolve(deps.resolveDecision(card.id, action))
        .then(function (result) {
          const resolved =
            result &&
            (result.outcome === "approved" || result.outcome === "dismissed");
          if (!resolved) restore(card.id, (result && result.error) || null);
          // On success we keep the optimistic removal; the confirming
          // ChangeEvent (kind:"updated") simply clears the pending entry.
        })
        .catch(function (err) {
          restore(card.id, err && err.message ? err.message : "Action failed");
        });
    }

    // Reconcile a failed action by restoring the card with an error (Req 14.6).
    function restore(id, errorMessage) {
      const pending = pendingRemovals.get(id);
      pendingRemovals.delete(id);
      if (!pending) return;
      const node = renderCard(pending.card);
      if (errorMessage) {
        const errEl = node.querySelector('[data-field="error"]');
        errEl.textContent = errorMessage;
        errEl.hidden = false;
      }
      listEl.appendChild(node);
      updateEmptyState();
    }

    // Change-event handler: add on created, reconcile removal on updated.
    function onChangeEvent(event) {
      if (!event || event.entity !== DECISION_ENTITY) return;
      if (event.kind === "created") {
        // Real-time add (Req 14.8): rebuild from the server so ordering and
        // the view-model stay authoritative.
        Promise.resolve(deps.fetchOpenDecisions()).then(render);
      } else if (event.kind === "updated" || event.kind === "removed") {
        // Confirms an optimistic removal; drop the pending entry so it is not
        // restored, and ensure the card is gone.
        pendingRemovals.delete(event.id);
        removeCardEl(event.id);
      }
    }

    function cssEscape(value) {
      if (global.CSS && typeof global.CSS.escape === "function") {
        return global.CSS.escape(value);
      }
      return String(value).replace(/["\\]/g, "\\$&");
    }

    function hydrate() {
      const dataEl = root.querySelector("#decisions-feed-data");
      let view = { cards: [], empty_state_message: emptyEl.textContent.trim() };
      if (dataEl) {
        try {
          view = JSON.parse(dataEl.textContent);
        } catch (e) {
          /* keep default empty view */
        }
      }
      render(view);
    }

    // Wire up.
    hydrate();
    const unsubscribe = deps.subscribe ? deps.subscribe(onChangeEvent) : null;

    return {
      render: render,
      onChangeEvent: onChangeEvent,
      destroy: function () {
        if (typeof unsubscribe === "function") unsubscribe();
      },
    };
  }

  global.createDecisionsFeed = createDecisionsFeed;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createDecisionsFeed: createDecisionsFeed };
  }
})(typeof window !== "undefined" ? window : this);

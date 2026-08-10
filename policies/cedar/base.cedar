// Agtyle baseline policy set.
//
// Agtyle is deny-by-default: an action with no matching permit is denied. Every policy carries
// a stable @id so a stored PolicyDecision can name exactly which policy determined the outcome.

// The Steward may create a reminder in the requesting user's own collection, but only when the
// user asked for it directly. Reminder creation is reversible and locally scoped, so it needs
// no approval; a denial here means denial, not an approval prompt.
@id("permit-steward-direct-reminder")
permit (
    principal == Agtyle::Agent::"steward",
    action == Agtyle::Action::"reminder.create",
    resource
)
when { context.has_direct_user_instruction == true }
when { resource.owner == context.origin_user_id };

// Sending mail leaves the system and is irreversible, so it requires an Approval that the
// kernel has already bound to this exact payload hash, principal, resource and expiry.
@id("permit-correspondence-approved-email-send")
permit (
    principal,
    action == Agtyle::Action::"email.send",
    resource
)
when { context.approval_present == true }
when { context.approval_valid == true }
when { resource.owner == context.origin_user_id };

// Money movement is prohibited outright in this deployment. A forbid always wins over a permit,
// so no future policy can accidentally re-enable it.
@id("forbid-any-finance-transfer")
forbid (
    principal,
    action == Agtyle::Action::"finance.transfer",
    resource
);

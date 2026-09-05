import {
  type GuardOptions,
  type VerifiedAction,
  verifyRoute,
  withVerifiedRoute,
} from "@402signal/route-guard";

const options: GuardOptions = {
  routeResponseJson: "{}",
  routeRequestJson: "{}",
  trustedLogVkey: "configured-pin",
  request: { url: "https://example.com/api", method: "GET" },
  challenge: { status: 402, bodyText: "{}" },
};
const action: VerifiedAction = verifyRoute(options);
const synchronous: string = withVerifiedRoute(options, (terms) => terms.model);
const asynchronous: Promise<string> = withVerifiedRoute(
  options,
  async (terms) => terms.request.url,
);
void synchronous;
void asynchronous;
// @ts-expect-error The selected terms must not be mutated after verification.
action.accepted.payTo = "other";
// @ts-expect-error The request must not be mutated after verification.
action.request.method = "POST";
// @ts-expect-error DELETE is outside the supported observed request profile.
options.request.method = "DELETE";

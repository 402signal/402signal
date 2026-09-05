export interface GuardOptions {
  /** Exact response text from 402Signal; retain the complete receipt and reveal. */
  routeResponseJson: string;
  /** Actual request sent to /route, including require_route_binding: true. */
  routeRequestJson: string;
  /** Independently configured C2SP Ed25519 log verification key. */
  trustedLogVkey: string;
  request: { url: string; method: "GET" | "POST"; body?: Uint8Array };
  /** Actual seller HTTP response, no redirects. Supply both channels if present. */
  challenge: {
    status: number;
    bodyText?: string;
    paymentRequired?: string;
    xPaymentRequired?: string;
  };
  /** Trusted Unix-seconds clock override for deterministic tests. */
  now?: number;
}
export interface VerifiedAction {
  readonly model: "proof_carrying_route_v1";
  readonly request: Readonly<{
    url: string;
    method: "GET" | "POST";
    body_sha256: string;
  }>;
  readonly accepted: Readonly<Record<string, unknown>>;
  readonly expires_at: number;
  readonly quote_sha256: string;
}
export class RouteGuardError extends Error {
  readonly code: string;
}
export function verifyRoute(options: GuardOptions): VerifiedAction;
export function withVerifiedRoute<T>(
  options: GuardOptions,
  authorize: (action: VerifiedAction) => T,
): T;

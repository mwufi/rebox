export interface ErrorDetail {
  /** Index of the offending item (claim or filter); -1 for request-level problems. */
  index: number;
  message: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: 400 | 401 | 403 | 404 | 503,
    readonly code: string,
    message: string,
    readonly details?: ErrorDetail[],
  ) {
    super(message);
  }

  body(): Record<string, unknown> {
    return this.details ? { error: this.code, details: this.details } : { error: this.code, message: this.message };
  }
}

export const validation = (details: ErrorDetail[]) => new ApiError(400, 'validation', 'validation failed', details);
export const badRequest = (message: string) => validation([{ index: -1, message }]);
export const notFound = (message = 'not found') => new ApiError(404, 'not_found', message);

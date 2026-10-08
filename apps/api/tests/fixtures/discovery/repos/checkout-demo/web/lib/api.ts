// Server-side api client. Only imported by server components and server actions, so
// API_URL (plain HTTP) never reaches the browser and there is no mixed-content problem.

export type Order = {
  id: number;
  customer_email: string;
  amount: string;
  status: string;
  created_at: string;
};

export class ApiError extends Error {}

function baseUrl(): string {
  const url = process.env.API_URL;
  if (!url) throw new ApiError("API_URL is not set on the server.");
  return url.replace(/\/+$/, "");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      ...init,
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError(`Could not reach the api (${(err as Error).name}).`);
  }
  if (!response.ok) {
    throw new ApiError(`The api returned ${response.status} for ${path}.`);
  }
  return (await response.json()) as T;
}

export function listOrders(limit = 20): Promise<Order[]> {
  return request<Order[]>(`/orders?limit=${limit}`);
}

export function checkout(customerEmail: string, amount: string): Promise<Order> {
  return request<Order>("/checkout", {
    method: "POST",
    body: JSON.stringify({ customer_email: customerEmail, amount }),
  });
}

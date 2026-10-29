import { PaymentClient } from '../generated/payment_client';
import type { PaymentRequest, ChargeResult } from '../generated/payment_client';

interface UsePaymentReturn {
  submitPayment: (req: PaymentRequest) => Promise<ChargeResult>;
  isLoading: boolean;
}

export function usePayment(): UsePaymentReturn {
  const client = new PaymentClient({ baseUrl: process.env.PAYMENT_API_URL });

  const submitPayment = async (req: PaymentRequest): Promise<ChargeResult> => {
    return client.createCharge(req);
  };

  return { submitPayment, isLoading: false };
}
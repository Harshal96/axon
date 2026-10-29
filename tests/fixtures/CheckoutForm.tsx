import React, { useState } from 'react';
import { Button, Input } from '@company/ui-components';
import { usePayment } from './usePayment';
import type { CheckoutFormProps } from './types';

interface FormState {
  cardNumber: string;
  expiry: string;
  cvv: string;
}

const CheckoutForm: React.FC<CheckoutFormProps> = ({ onSuccess, amount }) => {
  const [formState, setFormState] = useState<FormState>({
    cardNumber: '',
    expiry: '',
    cvv: '',
  });
  const { submitPayment, isLoading } = usePayment();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const result = await submitPayment({ ...formState, amount });
    if (result.success) {
      onSuccess(result.chargeId);
    }
  };

  return (
    <form onSubmit={handleSubmit}>
      <Input
        value={formState.cardNumber}
        onChange={(v) => setFormState(s => ({ ...s, cardNumber: v }))}
        placeholder="Card number"
      />
      <Button type="submit" disabled={isLoading}>
        {isLoading ? 'Processing...' : `Pay ${amount}`}
      </Button>
    </form>
  );
};

export default CheckoutForm;
export type { CheckoutFormProps };
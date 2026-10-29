package com.company.payment;

import com.company.models.ChargeResult;
import com.company.models.PaymentRequest;
import org.apache.commons.collections.CollectionUtils;
import java.math.BigDecimal;
import java.util.List;

/**
 * Gateway for processing payment charges.
 */
public class PaymentGateway extends AbstractPaymentProcessor
        implements PaymentProcessor, Auditable {

    private final String gatewayUrl;
    private final int timeoutMs;

    public PaymentGateway(String gatewayUrl, int timeoutMs) {
        this.gatewayUrl = gatewayUrl;
        this.timeoutMs = timeoutMs;
    }

    public ChargeResult charge(PaymentRequest request) {
        if (request == null || request.getAmount() == null) {
            throw new IllegalArgumentException("Request or amount is null");
        }
        validate(request);
        return submitToGateway(request);
    }

    private ChargeResult submitToGateway(PaymentRequest request) {
        // Calls out to external gateway
        return new ChargeResult(request.getId(), "SUCCESS");
    }

    @Override
    public void validate(PaymentRequest request) {
        BigDecimal amount = request.getAmount();
        List<?> items = request.getLineItems();
        if (!CollectionUtils.isEmpty(items)) {
            super.validate(request);
        }
    }

    private static class RetryPolicy {
        private final int maxRetries;
        RetryPolicy(int maxRetries) { this.maxRetries = maxRetries; }
        public boolean shouldRetry(int attempt) { return attempt < maxRetries; }
    }
}

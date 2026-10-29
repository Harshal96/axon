package com.company.payment;

public class PaymentService {
    private final PaymentGateway gateway;
    
    public PaymentService(PaymentGateway gateway) {
        this.gateway = gateway;
    }
    
    public void process() {
        gateway.charge(null);
    }
}

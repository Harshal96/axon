// Fixture Go orders service (parser input only).
package main

import "net/http"

func price(items []int) int { return 0 }

func createOrder(w http.ResponseWriter, r *http.Request) {
	total := price(nil)
	_ = total
	http.Post("http://payments/v1/charges", "application/json", nil)
}

func getOrder(w http.ResponseWriter, r *http.Request) {}

func main() {
	http.HandleFunc("POST /orders", createOrder)
	http.HandleFunc("/orders/{id}", getOrder)
}

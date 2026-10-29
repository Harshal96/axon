// Eval fixture: Go router-style registrations (parser input only).
package main

import "net/http"

func reserve() {}

func createOrder(w http.ResponseWriter, r *http.Request) {
	reserve()
	req, _ := http.NewRequest("PUT", "http://billing/v1/charges/{id}?source=orders", nil)
	_ = req
}

func getOrder(w http.ResponseWriter, r *http.Request) {}

func deleteOrder(w http.ResponseWriter, r *http.Request) {}

func main() {
	r := newRouter()
	r.POST("/orders", createOrder)
	r.GET("/orders/:id", getOrder)
	r.DELETE("/orders/:id", deleteOrder)
}

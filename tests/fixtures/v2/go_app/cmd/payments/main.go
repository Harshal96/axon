// Fixture Go payments service (parser input only).
package main

import "net/http"

func saveCharge() {}

func createCharge(w http.ResponseWriter, r *http.Request) { saveCharge() }

func main() {
	http.HandleFunc("POST /v1/charges", createCharge)
}

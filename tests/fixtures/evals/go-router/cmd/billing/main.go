// Eval fixture: Go billing endpoint (parser input only).
package main

import "net/http"

func replaceCharge(w http.ResponseWriter, r *http.Request) {}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("PUT /v1/charges/{id}", replaceCharge)
}

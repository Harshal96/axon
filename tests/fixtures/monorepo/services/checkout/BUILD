py_library(
    name = "checkout_lib",
    srcs = ["src/checkout.py"],
    deps = ["//services/payment:generate_payment_client_py"],
    visibility = ["//..."],
)

py_test(
    name = "checkout_test",
    srcs = ["src/checkout_test.py"],
    deps = [":checkout_lib"],
    testonly = True,
)

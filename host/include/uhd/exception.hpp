//
// Copyright 2010-2011 Ettus Research LLC
// Copyright 2018 Ettus Research, a National Instruments Company
//
// SPDX-License-Identifier: GPL-3.0-or-later
//

#ifndef INCLUDED_UHD_EXCEPTION_HPP
#define INCLUDED_UHD_EXCEPTION_HPP

#include <uhd/config.hpp>
#include <boost/current_function.hpp>
#include <stdexcept>
#include <string>

/*!
 * Define common exceptions used throughout the code:
 *
 * - The python built-in exceptions were used as inspiration.
 * - Exceptions inherit from std::exception to provide what().
 * - Exceptions inherit from uhd::exception to provide code().
 *
 * The code() provides an error code which allows the application
 * the option of printing a cryptic error message from the 1990s.
 *
 * The dynamic_clone() and dynamic_throw() methods allow us to:
 * catch an exception by dynamic type (i.e. derived class), save it,
 * and later rethrow it, knowing only the static type (i.e. base class),
 * and then finally to catch it again using the derived type.
 *
 * http://www.open-std.org/jtc1/sc22/wg21/docs/papers/2006/n2106.html
 */
namespace uhd {

/*! Base class of all UHD-specific exceptions.
 */
struct UHD_API exception : std::runtime_error
{
    exception(const std::string& what);
    virtual unsigned code(void) const            = 0;
    virtual exception* dynamic_clone(void) const = 0;
    virtual void dynamic_throw(void) const       = 0;
};

/*! Raised when an assert statement fails.
 *
 * This includes our own assertion macros, such as UHD_ASSERT_THROW().
 */
struct UHD_API assertion_error : exception
{
    assertion_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual assertion_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! The base class for exceptions that are raised when a key or index is
 * invalid.
 */
struct UHD_API lookup_error : exception
{
    lookup_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual lookup_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when a sequence index is out of range.
 */
struct UHD_API index_error : lookup_error
{
    index_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual index_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when a dictionary-like object is trying to be indexed by an
 *  invalid key.
 *
 * This includes the property tree.
 */
struct UHD_API key_error : lookup_error
{
    key_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual key_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when an operation or function is executed with a value of
 *  incorrect type.
 *
 * This might occur when values are being passed around as strings, but the
 * underlying code will need convert to a native type.
 */
struct UHD_API type_error : exception
{
    type_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual type_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when an operation or function receives an argument that has the
 * right type but an inappropriate or invalid value, and no other exception
 * is more specific.
 */
struct UHD_API value_error : exception
{
    value_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual value_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when a value is inappropriate because it can't be narrowed as
 * required.
 *
 * Mostly raised by uhd::narrow()
 */
struct UHD_API narrowing_error : value_error
{
    narrowing_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual narrowing_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when an error is detected that doesn't fall into any of the
 * categories.
 */
struct UHD_API runtime_error : exception
{
    runtime_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual runtime_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when an error occurs during a USB transaction.
 */
struct UHD_API usb_error : runtime_error
{
    int _code;
    usb_error(int code, const std::string& what);
    virtual unsigned code(void) const
    {
        return _code;
    };
    virtual usb_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when a function is stubbed out but doesn't actually do anything
 * useful.
 */
struct UHD_API not_implemented_error : runtime_error
{
    not_implemented_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual not_implemented_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Base class for errors that occur outside of UHD.
 */
struct UHD_API environment_error : exception
{
    environment_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual environment_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when an I/O operation fails for an I/O-related reason.
 */
struct UHD_API io_error : environment_error
{
    io_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual io_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when a function returns a system-related error.
 */
struct UHD_API os_error : environment_error
{
    os_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual os_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! \deprecated
 */
struct UHD_API system_error : exception
{
    system_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual system_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*! Raised when a parser encounters a syntax error.
 *
 * Within UHD, this is limited to Noc-Script execution.
 */
struct UHD_API syntax_error : exception
{
    syntax_error(const std::string& what);
    virtual unsigned code(void) const;
    virtual syntax_error* dynamic_clone(void) const;
    virtual void dynamic_throw(void) const;
};

/*!
 * Create a formatted string with throw-site information.
 * Fills in the function name, file name, and line number.
 * \param what the std::exception message
 * \return the formatted exception message
 */
#define UHD_THROW_SITE_INFO(what)                                                        \
    std::string(std::string(what) + "\n" + "  in " + std::string(BOOST_CURRENT_FUNCTION) \
                + "\n" + "  at " + std::string(__FILE__) + ":"                           \
                + BOOST_STRINGIZE(__LINE__) + "\n")

/*!
 * Throws an invalid code path exception with throw-site information.
 * Use this macro in places that code execution is not supposed to go.
 */
#define UHD_THROW_INVALID_CODE_PATH() \
    throw uhd::system_error(UHD_THROW_SITE_INFO("invalid code path"))

/*!
 * Assert the result of the code evaluation.
 * If the code evaluates to false, throw an assertion error.
 * \param code the code that resolved to a boolean
 */
#define UHD_ASSERT_THROW(code)                                      \
    {                                                               \
        if (not(code))                                              \
            throw uhd::assertion_error(UHD_THROW_SITE_INFO(#code)); \
    }

} // namespace uhd

#endif /* INCLUDED_UHD_EXCEPTION_HPP */

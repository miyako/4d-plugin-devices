#include "4DPluginAPI.h"
#include "PrivateTypes.h"
#include "EntryPoints.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <unordered_map>

extern Call4DProcPtr gCall4D;

namespace {
bool allocatedEmpty = true;
std::unordered_map<PA_Unichar*, size_t> strings;
std::unordered_map<PA_Handle, size_t> handles;
struct Picture {
    size_t refs;
    std::vector<uint8_t> bytes;
};
std::unordered_map<PA_Picture, Picture> pictures;
CUTF16String expectedPath;
CUTF16String expectedType;
size_t commandCalls = 0;
size_t conversionCalls = 0;

void require(bool condition, const char* message)
{
    if (!condition) throw std::runtime_error(message);
}

CUTF16String utf16(const char* bytes)
{
    CFStringRef s = CFStringCreateWithBytes(kCFAllocatorDefault,
        reinterpret_cast<const UInt8*>(bytes), std::strlen(bytes),
        kCFStringEncodingUTF8, false);
    require(s != nullptr, "invalid synthetic UTF-8 fixture");
    CUTF16String result(CFStringGetLength(s), 0);
    if (!result.empty())
        CFStringGetCharacters(s, CFRangeMake(0, result.size()), &result[0]);
    CFRelease(s);
    return result;
}

PA_Unistring makeString(const PA_Unichar* chars)
{
    size_t length = 0;
    if (chars) while (chars[length]) ++length;
    PA_Unistring result = {};
    if (length || allocatedEmpty) {
        result.fString = new PA_Unichar[length + 1];
        std::copy_n(chars, length, result.fString);
        result.fString[length] = 0;
        PA_Unichar* buffer = result.fString;
        strings.emplace(buffer, length);
    }
    result.fLength = static_cast<PA_long32>(length);
    return result;
}

void disposeString(PA_Unistring str)
{
    if (!str.fString) {
        require(str.fLength == 0, "null nonempty string");
        return;
    }
    PA_Unichar* buffer = str.fString;
    auto it = strings.find(buffer);
    require(it != strings.end(), "duplicate or borrowed string disposal");
    require(it->second == static_cast<size_t>(str.fLength), "string length changed");
    delete[] str.fString;
    strings.erase(it);
}

CUTF16String readString(PA_Unistring str)
{
    if (!str.fString) {
        require(str.fLength == 0, "null nonempty read");
        return {};
    }
    PA_Unichar* buffer = str.fString;
    auto it = strings.find(buffer);
    require(it != strings.end(), "read after string disposal");
    require(it->second == static_cast<size_t>(str.fLength), "incorrect descriptor length");
    require(str.fString[str.fLength] == 0, "missing UTF-16 terminator");
    return CUTF16String(str.fString, str.fLength);
}

PA_Picture makePicture(const void* bytes, size_t length)
{
    auto* identity = new uint8_t;
    Picture picture = {1, {}};
    if (length) {
        require(bytes != nullptr, "null nonempty picture");
        const auto* begin = static_cast<const uint8_t*>(bytes);
        picture.bytes.assign(begin, begin + length);
    }
    pictures.emplace(identity, std::move(picture));
    return identity;
}

Picture& readPicture(PA_Picture p)
{
    auto it = pictures.find(p);
    require(it != pictures.end(), "read after picture disposal");
    return it->second;
}

void disposePicture(PA_Picture p)
{
    if (!p) return;
    auto it = pictures.find(p);
    require(it != pictures.end(), "duplicate picture disposal");
    require(it->second.refs > 0, "invalid picture reference count");
    if (--it->second.refs == 0) {
        pictures.erase(it);
        delete static_cast<uint8_t*>(p);
    }
}

void clear(PA_Variable& v)
{
    require(v.fFiller == 0, "metadata reference must not be blindly cleared");
    switch (v.fType) {
        case eVK_Unistring: disposeString(v.uValue.fString); break;
        case eVK_Picture: disposePicture(v.uValue.fPicture); break;
        case eVK_ArrayUnicode: {
            PA_Handle h = v.uValue.fArray.fData;
            if (h) {
                require(handles.count(h), "clearing unknown array handle");
                require(handles.at(h) == (v.uValue.fArray.fNbElements + 1) *
                    sizeof(PA_Unistring), "array count exceeds allocation");
                auto* elements = reinterpret_cast<PA_Unistring*>(*h);
                for (PA_long32 i = 0; i <= v.uValue.fArray.fNbElements; ++i)
                    disposeString(elements[i]);
                std::free(*h);
                delete h;
                handles.erase(h);
            }
            break;
        }
        case eVK_Undefined:
        case eVK_Longint: break;
        default: throw std::runtime_error("unexpected variable kind in shim");
    }
    v = {};
    v.fType = eVK_Undefined;
}

void replacePicture(PA_Variable& v, const std::vector<uint8_t>& bytes)
{
    clear(v);
    PA_SetPictureVariable(&v, makePicture(bytes.data(), bytes.size()));
}

void host(short selector, EngineBlock* eb)
{
    switch (selector) {
        case EX_CREATE_UNISTRING:
            eb->fUniString1 = makeString(reinterpret_cast<PA_Unichar*>(eb->fHandle));
            break;
        case EX_DISPOSE_UNISTRING: disposeString(eb->fUniString1); break;
        case EX_SET_UNISTRING: {
            // Content setter reads the source before replacing the old owner.
            PA_Unistring copy = makeString(reinterpret_cast<PA_Unichar*>(eb->fHandle));
            disposeString(eb->fUniString1);
            eb->fUniString1 = copy;
            break;
        }
        case EX_HANDLE_MANAGER: {
            PA_Handle handle = eb->fParam1 == 1 ? nullptr : eb->fHandle;
            sLONG_PTR requested = (eb->fParam1 == 1 || eb->fParam1 == 3) ? eb->fParam2 : 0;
            switch (eb->fParam1) {
                case 1: {
                    PA_Handle h = new char*;
                    *h = static_cast<char*>(std::calloc(1, std::max<sLONG_PTR>(1, requested)));
                    require(*h != nullptr, "test handle allocation failed");
                    handles.emplace(h, requested);
                    eb->fHandle = h;
                    break;
                }
                case 2:
                    require(handles.count(handle), "duplicate handle disposal");
                    std::free(*handle);
                    delete handle;
                    handles.erase(handle);
                    break;
                case 3: {
                    require(handles.count(handle), "resizing unknown handle");
                    void* p = std::realloc(*handle, std::max<sLONG_PTR>(1, requested));
                    require(p != nullptr, "test handle resize failed");
                    *handle = static_cast<char*>(p);
                    handles[handle] = requested;
                    break;
                }
                case 4:
                    require(handles.count(handle), "sizing unknown handle");
                    eb->fParam2 = handles.at(handle);
                    break;
                case 5:
                    require(handles.count(handle), "locking unknown handle");
                    eb->fParam3 = reinterpret_cast<sLONG_PTR>(*handle);
                    break;
                case 6: require(handles.count(handle), "unlocking unknown handle"); break;
                default: throw std::runtime_error("unexpected handle operation");
            }
            eb->fError = 0;
            break;
        }
        case EX_CLEAR_VARIABLE: clear(*static_cast<PA_Variable*>(eb->fPtr1)); break;
        case EX_CREATE_PICTURE:
            eb->fPicture = makePicture(eb->fPtr1, eb->fParam1);
            eb->fError = 0;
            break;
        case EX_DISPOSE_PICTURE: disposePicture(eb->fPicture); break;
        case EX_DUPLICATE_PICTURE:
            if (eb->fPicture) {
                Picture& p = readPicture(eb->fPicture);
                if (eb->fParam1 == 0) ++p.refs;
                else eb->fPicture = makePicture(p.bytes.data(), p.bytes.size());
            }
            break;
        case EX_GET_PICTURE_DATA:
            readPicture(eb->fPicture);
            // No representations: do not assign ownership to the undocumented output.
            eb->fUniString1 = {};
            eb->fError = eER_IncompatibleTypes;
            break;
        case EX_CONVERT_CHARSET_TO_CHARSET: {
            require(eb->fParam2 == eVTC_UTF_8 && eb->fParam4 == eVTC_UTF_16,
                "unexpected charset conversion");
            CFStringRef s = CFStringCreateWithBytes(kCFAllocatorDefault,
                static_cast<UInt8*>(eb->fPtr1), eb->fParam1, kCFStringEncodingUTF8, false);
            require(s != nullptr, "invalid charset input");
            CFIndex units = CFStringGetLength(s);
            CFIndex bytes = units * sizeof(PA_Unichar);
            require(bytes <= eb->fParam3 &&
                eb->fParam3 == eb->fParam1 * sizeof(PA_Unichar) + sizeof(PA_Unichar),
                "UTF-16 capacity is measured in bytes");
            // Only converted bytes are promised by this SDK. Do not rely on
            // a converter terminator or zero-filled spare buffer capacity.
            std::memset(eb->fPtr2, 0xa5, eb->fParam3);
            if (units) CFStringGetCharacters(s, CFRangeMake(0, units),
                static_cast<UniChar*>(eb->fPtr2));
            CFRelease(s);
            eb->fParam3 = bytes;
            eb->fError = 0;
            ++conversionCalls;
            break;
        }
        case EX_EXECUTE_COMMAND_BY_ID: {
            ++commandCalls;
            auto** args = static_cast<PA_Variable**>(eb->fPtr1);
            switch (eb->fParam1) {
                case 678:
                    require(eb->fParam2 == 2, "READ PICTURE FILE arity");
                    require(args[0]->fType == eVK_Unistring, "file path type");
                    require(readString(args[0]->uValue.fString) == expectedPath,
                        "file path content mismatch");
                    replacePicture(*args[1], {1, 2, 3, 4});
                    break;
                case 1002:
                    require(eb->fParam2 == 2, "CONVERT PICTURE arity");
                    require(readString(args[1]->uValue.fString) == expectedType,
                        "picture format content mismatch");
                    readPicture(PA_GetPictureVariable(*args[0]));
                    replacePicture(*args[0], {5, 6, 7});
                    break;
                case 988:
                    require(eb->fParam2 == 2, "TRANSFORM PICTURE arity");
                    require(PA_GetLongintVariable(*args[1]) == 101, "grayscale operation");
                    readPicture(PA_GetPictureVariable(*args[0]));
                    replacePicture(*args[0], {8, 9});
                    break;
                case 457:
                    require(eb->fParam2 == 3, "PICTURE PROPERTIES arity");
                    readPicture(PA_GetPictureVariable(*args[0]));
                    PA_SetLongintVariable(args[1], 32);
                    PA_SetLongintVariable(args[2], 24);
                    break;
                case 679:
                    require(eb->fParam2 == 4, "CREATE THUMBNAIL arity");
                    readPicture(PA_GetPictureVariable(*args[0]));
                    require(PA_GetLongintVariable(*args[2]) == 16 &&
                        PA_GetLongintVariable(*args[3]) == 12, "thumbnail dimensions");
                    replacePicture(*args[1], {10, 11});
                    break;
                default: throw std::runtime_error("unexpected command or device access");
            }
            auto* returned = static_cast<PA_Variable*>(eb->fPtr2);
            returned->fType = eVK_Undefined; // These picture commands are procedures.
            returned->fFiller = 0;
            eb->fError = 0;
            break;
        }
        default: throw std::runtime_error("unexpected engine selector");
    }
}

void balance()
{
    require(strings.empty(), "unbalanced string allocations (including allocated empties)");
    require(handles.empty(), "unbalanced handles");
    require(pictures.empty(), "unbalanced picture references (including allocated empties)");
}

const char* fixtures[] = {"ASCII", "\xe6\x97\xa5\xe6\x9c\xac",
    "A\xf0\x9f\x98\x80Z", ""};

void pathTest(bool emptyOnly = false)
{
    for (const char* fixture : fixtures) {
        if (emptyOnly && *fixture) continue;
        expectedPath = utf16(fixture);
        CUTF8String path(reinterpret_cast<const uint8_t*>(fixture));
        {
            CPicture picture;
            picture.fromFileAtPath(&path);
            require(readPicture(picture.getPicture()).bytes ==
                std::vector<uint8_t>({1, 2, 3, 4}), "file output did not survive argument clear");
        }
        balance();
    }
    require(commandCalls == (emptyOnly ? 1 : 4), "path commands not executed");
}

void bytesTest()
{
    const uint8_t bytes[] = {42, 0, 255};
    {
        CPicture picture;
        for (int i = 0; i < 10; ++i) {
            picture.setBytes(bytes, sizeof(bytes));
            require(readPicture(picture.getPicture()).bytes ==
                std::vector<uint8_t>(bytes, bytes + sizeof(bytes)), "picture bytes mismatch");
        }
        picture.setBytes(nullptr, 0);
        require(readPicture(picture.getPicture()).bytes.empty(), "empty picture mismatch");
    }
    balance();
}

void copyTest(bool emptyOnly = false)
{
    for (const char* fixture : fixtures) {
        if (emptyOnly && *fixture) continue;
        expectedType = utf16(fixture);
        PA_Picture output;
        {
            CPicture picture;
            output = picture.createCopy(fixture);
            require(readPicture(output).bytes == std::vector<uint8_t>({5, 6, 7}),
                "converted output mismatch");
        }
        require(readPicture(output).refs == 1, "owning conversion handoff did not survive");
        PA_DisposePicture(output);
        balance();
    }
    require(conversionCalls == (emptyOnly ? 1 : 4) &&
        commandCalls == conversionCalls, "conversion path not executed");
}

void grayTest()
{
    PA_Picture output;
    {
        CPicture picture;
        output = picture.createGrayScale();
    }
    require(readPicture(output).bytes == std::vector<uint8_t>({8, 9}),
        "owning grayscale handoff did not survive");
    PA_DisposePicture(output);
    balance();
}

void sizeTest()
{
    {
        CPicture picture;
        unsigned int width = 0, height = 0;
        picture.getSize(&width, &height);
        require(width == 32 && height == 24, "picture dimensions mismatch");
        require(readPicture(picture.getPicture()).refs == 1,
            "borrowed picture input lost ownership");
    }
    balance();
}

void thumbnailTest()
{
    PA_Picture output;
    {
        CPicture picture;
        output = picture.createThumbnail(16, 12);
        require(readPicture(picture.getPicture()).refs == 1, "thumbnail borrowed input disposed");
    }
    require(readPicture(output).bytes == std::vector<uint8_t>({10, 11}),
        "owning thumbnail handoff did not survive");
    PA_DisposePicture(output);
    balance();
}

void arrayTest()
{
    PA_Variable array = PA_CreateVariable(eVK_ArrayUnicode);
    PA_ResizeArray(&array, 4);
    for (int i = 0; i < 4; ++i) {
        CUTF16String text = utf16(fixtures[i]);
        PA_Unistring owned = PA_CreateUnistring(const_cast<PA_Unichar*>(text.c_str()));
        PA_SetStringInArray(array, i, &owned);
        require(readString(PA_GetStringInArray(array, i)) == text, "accepted transfer mismatch");
    }
    ARRAY_TEXT copied;
    BytePtr params[] = {reinterpret_cast<BytePtr>(&array)};
    copied.fromParamAtIndex(params, 1);
    CUTF16String old = utf16(fixtures[2]);
    PA_Unistring borrowed = PA_GetStringInArray(array, 2);
    PA_Unistring independent = PA_CreateUnistring(borrowed.fString);
    PA_SetStringInArray(array, 4, &independent);
    PA_Unistring replacement = PA_CreateUnistring(nullptr);
    PA_SetStringInArray(array, 2, &replacement);
    require(readString(PA_GetStringInArray(array, 4)) == old, "independent transfer invalidated");
    PA_ClearVariable(&array);
    for (int i = 0; i < 4; ++i) {
        CUTF16String actual;
        copied.copyUTF16StringAtIndex(&actual, i);
        require(actual == utf16(fixtures[i]), "borrowed array was not copied before owner clear");
    }
    PA_Variable outputs[6];
    for (auto& output : outputs) {
        output = PA_CreateVariable(eVK_Undefined);
        BytePtr destination[] = {reinterpret_cast<BytePtr>(&output)};
        copied.toParamAtIndex(destination, 1);
    }
    for (int pass = 0; pass < 6; ++pass) {
        array = outputs[pass];
        std::vector<std::vector<uint8_t>> churn(64, std::vector<uint8_t>(1024, 0xa5));
        for (int i = 0; i < 4; ++i)
            require(readString(PA_GetStringInArray(array, i)) == utf16(fixtures[i]),
                "wrapper output content mismatch");
        if (pass < 5)
            require(PA_GetStringInArray(array, 0).fString !=
                PA_GetStringInArray(outputs[pass + 1], 0).fString,
                "separate output arrays share an owning allocation");
        // Replacement disposes the old destination, not any sibling array.
        copied.toParamAtIndex(params, 1);
        PA_ResizeArray(&array, 1);
        PA_ClearVariable(&array);
    }
    // Rejections retain the caller's allocation, including allocated empty input.
    for (int which = 0; which < 4; ++which) {
        array = PA_CreateVariable(eVK_ArrayUnicode);
        PA_Variable target = array;
        if (which == 0) target.fType = eVK_Longint;
        if (which == 1) target.uValue.fArray.fData = nullptr;
        CUTF16String text = utf16(fixtures[which]);
        PA_Unistring owned = PA_CreateUnistring(const_cast<PA_Unichar*>(text.c_str()));
        PA_SetStringInArray(target, which == 2 ? -1 : which == 3 ? 1 : 0, &owned);
        require(readString(owned) == text, "rejected transfer consumed input");
        PA_DisposeUnistring(&owned);
        PA_Unistring before = PA_GetStringInArray(array, 0);
        PA_SetStringInArray(array, 0, nullptr);
        PA_Unistring after = PA_GetStringInArray(array, 0);
        require(before.fString == after.fString && readString(before) == readString(after),
            "null descriptor changed the destination");
        PA_ClearVariable(&array);
    }
    balance();
}

void scalarTest()
{
    for (const char* fixture : fixtures) {
        CUTF16String expected = utf16(fixture);
        PA_Unistring source = PA_CreateUnistring(const_cast<PA_Unichar*>(expected.c_str()));
        C_TEXT copy;
        copy.setUTF16String(&source);
        PA_DisposeUnistring(&source);
        PA_Unistring output = PA_CreateUnistring(nullptr);
        BytePtr params[] = {reinterpret_cast<BytePtr>(&output)};
        copy.toParamAtIndex(params, 1);
        require(readString(output) == expected, "scalar copied content mismatch");
        PA_Unistring returned = PA_CreateUnistring(nullptr);
        PA_Unistring* returnPtr = &returned;
        copy.setReturn(reinterpret_cast<sLONG_PTR*>(&returnPtr));
        require(readString(returned) == expected, "scalar return content mismatch");
        PluginBlock block = {};
        block.fResult = &returnPtr;
        PA_ReturnString(&block, const_cast<PA_Unichar*>(expected.c_str()));
        require(readString(returned) == expected, "SDK return content mismatch");
        // Independent synchronous argument, cleared after its last read.
        PA_Unistring arg = PA_CreateUnistring(const_cast<PA_Unichar*>(expected.c_str()));
        PA_Variable argument = PA_CreateVariable(eVK_Undefined);
        PA_SetStringVariable(&argument, &arg);
        require(readString(argument.uValue.fString) == expected, "scalar shallow assignment mismatch");
        PA_ClearVariable(&argument);
        require(readString(output) == expected && readString(returned) == expected,
            "independent scalar owners aliased");
        PA_DisposeUnistring(&output);
        PA_DisposeUnistring(&returned);
        balance();
    }
}
}

int main(int argc, char** argv)
{
    try {
        require(argc == 3, "expected case and empty representation");
        allocatedEmpty = std::string(argv[2]) == "allocated";
        gCall4D = host;
        std::string test = argv[1];
        if (test == "picture-path") pathTest();
        else if (test == "picture-path-empty") pathTest(true);
        else if (test == "picture-bytes") bytesTest();
        else if (test == "picture-copy") copyTest();
        else if (test == "picture-copy-empty") copyTest(true);
        else if (test == "picture-gray") grayTest();
        else if (test == "picture-size") sizeTest();
        else if (test == "picture-thumbnail") thumbnailTest();
        else if (test == "array") arrayTest();
        else if (test == "scalar") scalarTest();
        else throw std::runtime_error("unknown test");
        balance();
        std::cout << "PASS " << test << " " << argv[2] << "\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "FAIL " << (argc > 1 ? argv[1] : "") << ": " << e.what() << "\n";
        return 1;
    }
}
